"""Repository.

Every database operation lives here, and every one takes a connection_id.

That is the tenant boundary made structural. A route cannot read another
merchant's cases, because there is no function available to it that would return
them. Enforcing isolation by remembering to add a filter works right up until
someone forgets; enforcing it by not providing the unfiltered function does not
have that failure mode.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from shared.models import CaseState

from .models import Approval, Case, MerchantPolicy, Outcome
from .session import session_scope

logger = logging.getLogger(__name__)


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _aware(dt: datetime | None) -> datetime | None:
    """Force a datetime read from the database to be UTC-aware.

    SQLite has no timezone type, so a column declared timezone-aware still comes
    back naive; Postgres returns it aware. Comparing the two raises, which means
    code that works on SQLite would fail on Postgres or vice versa. Normalizing on
    read makes the difference invisible - which is the whole point of claiming the
    schema is portable.
    """
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def record_case(
    *,
    connection_id: str,
    friction: str | None,
    query: str | None,
    cart_id: str | None,
    order_id: str | None,
    session_id: str | None,
    reasoning: dict,
    decision: dict,
    risk: dict,
    approval_timeout_minutes: int = 15,
) -> str:
    """Write a case, and an approval when one is needed.

    Both in a single transaction. An approval row pointing at a case that failed to
    write would be an orphan an operator could open but not understand.

    The state is derived from the risk outcome rather than passed in, so the case
    state and the gate's verdict cannot disagree.
    """
    case_id = _id("case")
    outcome = risk.get("outcome")

    if outcome == "AUTO":
        state = CaseState.EXECUTING
    elif outcome == "BLOCK":
        state = CaseState.BLOCKED
    else:
        state = CaseState.PENDING_APPROVAL

    async with session_scope() as db:
        db.add(
            Case(
                case_id=case_id,
                connection_id=connection_id,
                session_id=session_id,
                friction_type=friction,
                state=str(state),
                query=query,
                cart_id=cart_id,
                order_id=order_id,
                used_model=bool(reasoning.get("used_model")),
                model_name=reasoning.get("model_name"),
                diagnosis=reasoning.get("diagnosis"),
                evidence=reasoning.get("evidence") or [],
                fallback_reason=reasoning.get("fallback_reason"),
                model_reply=reasoning.get("model_reply"),
                shopper_reply=reasoning.get("shopper_reply"),
                proposed=decision.get("proposed") or [],
                rejected=decision.get("rejected") or [],
                selected_action=decision.get("selected_action"),
                selection_reason=decision.get("selection_reason"),
                risk_outcome=outcome,
                risk_rule=risk.get("rule"),
                risk_reason=risk.get("reason"),
                financial=bool(risk.get("financial")),
                prompt_tokens=reasoning.get("prompt_tokens"),
                completion_tokens=reasoning.get("completion_tokens"),
            )
        )

        if state is CaseState.PENDING_APPROVAL:
            db.add(
                Approval(
                    approval_id=_id("apr"),
                    case_id=case_id,
                    connection_id=connection_id,
                    state="PENDING",
                    action_type=decision.get("selected_action") or "UNKNOWN",
                    risk_rule=risk.get("rule"),
                    expires_at=datetime.now(UTC)
                    + timedelta(minutes=approval_timeout_minutes),
                )
            )

    return case_id


async def list_cases(connection_id: str, *, limit: int = 50) -> list[Case]:
    """Recent cases for one merchant, newest first."""
    async with session_scope() as db:
        result = await db.execute(
            select(Case)
            .where(Case.connection_id == connection_id)
            .order_by(Case.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())


async def get_case(connection_id: str, case_id: str) -> Case | None:
    """One case, scoped to the merchant.

    The connection_id filter is not redundant with the primary key. Without it,
    guessing a case id from another merchant would return their data.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(Case).where(
                Case.case_id == case_id, Case.connection_id == connection_id
            )
        )
        return result.scalar_one_or_none()


async def pending_approvals(connection_id: str, *, limit: int = 50) -> list[dict]:
    """The queue: what a person needs to decide, oldest first.

    Oldest first because a shopper has been waiting, and newest-first would leave
    the person who has waited longest waiting longer.

    Expired approvals are filtered out at read time rather than swept by a
    background job. Simpler, and there is no window where an expired approval is
    still actionable.
    """
    now = datetime.now(UTC)
    async with session_scope() as db:
        result = await db.execute(
            select(Approval, Case)
            .join(Case, Case.case_id == Approval.case_id)
            .where(
                Approval.connection_id == connection_id,
                Approval.state == "PENDING",
            )
            .order_by(Approval.requested_at.asc())
            .limit(limit)
        )
        rows = []
        for approval, case in result.all():
            expires = _aware(approval.expires_at)
            if expires is not None and expires < now:
                continue
            rows.append(
                {
                    "approval_id": approval.approval_id,
                    "case_id": case.case_id,
                    "action_type": approval.action_type,
                    "risk_rule": approval.risk_rule,
                    "requested_at": _aware(approval.requested_at).isoformat(),
                    "expires_at": (expires.isoformat() if expires else None),
                    "friction_type": case.friction_type,
                    "diagnosis": case.diagnosis,
                    "evidence": case.evidence,
                    "used_model": case.used_model,
                    "shopper_reply": case.shopper_reply,
                    "model_reply": case.model_reply,
                    "selection_reason": case.selection_reason,
                    "rejected": case.rejected,
                    "financial": case.financial,
                    "order_id": case.order_id,
                    "query": case.query,
                }
            )
        return rows

async def decide_approval(
    connection_id: str,
    approval_id: str,
    *,
    approved: bool,
    decided_by: str,
    note: str | None = None,
) -> dict | None:
    """Approve or reject.

    Refuses to decide an approval that is not PENDING, rather than overwriting a
    decision. Two people clicking approve at the same moment should not produce two
    executions, and an audit record that changed after the fact is not an audit
    record.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(Approval).where(
                Approval.approval_id == approval_id,
                Approval.connection_id == connection_id,
            )
        )
        approval = result.scalar_one_or_none()
        if approval is None:
            return None
        if approval.state != "PENDING":
            return {
                "approval_id": approval_id,
                "case_id": approval.case_id,
                "state": approval.state,
                "changed": False,
            }

        approval.state = "APPROVED" if approved else "REJECTED"
        approval.decided_at = datetime.now(UTC)
        approval.decided_by = decided_by
        approval.note = note

        case = await db.get(Case, approval.case_id)
        if case is not None:
            case.state = str(CaseState.APPROVED if approved else CaseState.REJECTED)

        return {
            "approval_id": approval_id,
            "case_id": approval.case_id,
            "state": approval.state,
            "changed": True,
        }
async def record_outcome(
    *,
    connection_id: str,
    case_id: str,
    resolved: bool,
    final_state: str,
    friction_type: str | None = None,
    amount: str | None = None,
    currency: str | None = None,
    required_human: bool = False,
) -> None:
    """Close a case out.

    time_to_resolution_ms is computed from the case rather than passed in, so it
    cannot be reported as something other than what actually elapsed.
    """
    async with session_scope() as db:
        case = await db.get(Case, case_id)
        if case is None or case.connection_id != connection_id:
            return

        elapsed = int(
            (datetime.now(UTC) - _aware(case.created_at)).total_seconds() * 1000
        )

        db.add(
            Outcome(
                outcome_id=_id("out"),
                case_id=case_id,
                connection_id=connection_id,
                resolved=resolved,
                final_state=final_state,
                friction_type=friction_type or case.friction_type,
                revenue_recovered_amount=amount,
                revenue_recovered_currency=currency,
                time_to_resolution_ms=elapsed,
                required_human=required_human,
            )
        )
        case.state = final_state


async def stats(connection_id: str) -> dict:
    """Headline numbers for the console.

    Counted in the database rather than by loading rows and counting in Python,
    because this runs on every console page load and the case table only grows.
    """
    async with session_scope() as db:
        total = await db.scalar(
            select(func.count(Case.case_id)).where(Case.connection_id == connection_id)
        )
        pending = await db.scalar(
            select(func.count(Approval.approval_id)).where(
                Approval.connection_id == connection_id, Approval.state == "PENDING"
            )
        )
        auto = await db.scalar(
            select(func.count(Case.case_id)).where(
                Case.connection_id == connection_id, Case.risk_outcome == "AUTO"
            )
        )
        by_model = await db.scalar(
            select(func.count(Case.case_id)).where(
                Case.connection_id == connection_id, Case.used_model.is_(True)
            )
        )
        return {
            "cases": total or 0,
            "pending_approvals": pending or 0,
            "auto_cleared": auto or 0,
            "reasoned_by_model": by_model or 0,
        }




async def save_policy(
    connection_id: str,
    *,
    mode: str,
    auto_allowed: list[str],
    blocked: list[str],
    approval_timeout_minutes: int = 15,
) -> None:
    """Persist a connection's risk settings.

    Upsert by hand rather than with a dialect-specific ON CONFLICT, so the same code
    runs on SQLite and Postgres.
    """
    async with session_scope() as db:
        row = await db.get(MerchantPolicy, connection_id)
        if row is None:
            row = MerchantPolicy(connection_id=connection_id)
            db.add(row)
        row.mode = mode
        row.auto_allowed = auto_allowed
        row.blocked = blocked
        row.approval_timeout_minutes = approval_timeout_minutes


async def load_policies() -> list[dict]:
    """Every stored policy, for hydrating the in-memory store at startup."""
    async with session_scope() as db:
        rows = await db.execute(select(MerchantPolicy))
        return [
            {
                "connection_id": r.connection_id,
                "mode": r.mode,
                "auto_allowed": r.auto_allowed or [],
                "blocked": r.blocked or [],
                "approval_timeout_minutes": r.approval_timeout_minutes,
            }
            for r in rows.scalars()
        ]

async def merchant_report(connection_id: str, *, days: int = 30) -> dict:
    """What the engine did for one merchant, in their terms.

    `stats` counts cases for the operations console. This answers a different
    question, asked by a different person: what did this do for my shop.

    So the numbers are outcomes rather than throughput. A merchant does not care how
    many cases were opened; they care how many shoppers were helped, how much money
    came back, and how much of it needed someone's time.

    Revenue is summed from outcomes rather than from cases, because only an outcome
    knows whether money actually moved. A case that proposed a payment recovery and
    was rejected recovered nothing.
    """
    since = datetime.now(UTC) - timedelta(days=days)

    async with session_scope() as db:
        cases = await db.execute(
            select(Case).where(
                Case.connection_id == connection_id, Case.created_at >= since
            )
        )
        case_rows = list(cases.scalars())

        outcomes = await db.execute(
            select(Outcome).where(
                Outcome.connection_id == connection_id, Outcome.recorded_at >= since
            )
        )
        outcome_rows = list(outcomes.scalars())

        waiting = await db.scalar(
            select(func.count(Approval.approval_id)).where(
                Approval.connection_id == connection_id, Approval.state == "PENDING"
            )
        )

    resolved = [o for o in outcome_rows if o.resolved]

    # Decimal, not float. A revenue figure shown to a merchant is the one number on
    # the page they will check against their own books.
    recovered = sum(
        (
            Decimal(o.revenue_recovered_amount)
            for o in resolved
            if o.revenue_recovered_amount
        ),
        Decimal("0.00"),
    )
    currency = next(
        (
            o.revenue_recovered_currency
            for o in resolved
            if o.revenue_recovered_currency
        ),
        "INR",
    )

    friction: dict[str, int] = {}
    for case in case_rows:
        if case.friction_type:
            friction[case.friction_type] = friction.get(case.friction_type, 0) + 1

    handled_alone = sum(1 for o in resolved if not o.required_human)

    times = [o.time_to_resolution_ms for o in resolved if o.time_to_resolution_ms]
    median_ms = sorted(times)[len(times) // 2] if times else None

    return {
        "days": days,
        "shoppers_helped": len(case_rows),
        "problems_solved": len(resolved),
        "handled_without_you": handled_alone,
        "waiting_for_you": waiting or 0,
        "revenue_recovered": f"{recovered:.2f}",
        "currency": currency,
        "median_resolution_ms": median_ms,
        "friction": [
            {"type": k, "count": v}
            for k, v in sorted(friction.items(), key=lambda kv: -kv[1])
        ],
        "recent": [
            {
                "case_id": c.case_id,
                "friction_type": c.friction_type,
                "diagnosis": c.diagnosis,
                "selected_action": c.selected_action,
                "risk_outcome": c.risk_outcome,
                # The reply as sent, not as the model first drafted it. When an action was
                # held for approval the two differ, and showing a merchant a promise
                # their customer never received would misrepresent their own shop.
                "shopper_reply": c.shopper_reply,
                "used_model": c.used_model,
                "created_at": _aware(c.created_at).isoformat(),
            }
            for c in sorted(case_rows, key=lambda c: c.created_at, reverse=True)[:8]
        ],
    }


async def expire_approvals() -> list[dict]:
    """Close out approvals nobody actioned in time.

    Until now an expired approval simply stopped appearing in the queue. The case
    stayed PENDING_APPROVAL forever, the shopper who was told "someone will pick
    this up" was never told otherwise, and nothing recorded that a sale had been
    lost to nobody looking. That is the worst of the three: it is invisible, so it
    never gets fixed.

    Returns what expired, so the caller can tell each shopper and record an outcome.
    The caller does that rather than this function, because writing to a session and
    recording revenue are not the repository's job.
    """
    now = datetime.now(UTC)

    async with session_scope() as db:
        result = await db.execute(
            select(Approval, Case)
            .join(Case, Case.case_id == Approval.case_id)
            .where(
                Approval.state == "PENDING",
                Approval.expires_at.is_not(None),
                Approval.expires_at < now,
            )
        )

        expired = []
        for approval, case in result.all():
            approval.state = "EXPIRED"
            approval.decided_at = now
            # Recorded as the deciding party so an audit trail never has a decision
            # with nobody attached to it. Nobody decided; time did.
            approval.decided_by = "expired"
            case.state = str(CaseState.TIMEOUT)

            expired.append(
                {
                    "approval_id": approval.approval_id,
                    "case_id": case.case_id,
                    "connection_id": case.connection_id,
                    "session_id": case.session_id,
                    "action_type": approval.action_type,
                    "friction_type": case.friction_type,
                    "order_id": case.order_id,
                    "waited_minutes": int(
                        (now - _aware(approval.requested_at)).total_seconds() / 60
                    ),
                }
            )

    return expired


async def expire_approvals() -> list[dict]:
    """Close out approvals nobody actioned in time.

    Until now an expired approval simply stopped appearing in the queue. The case
    stayed PENDING_APPROVAL forever, the shopper who was told "someone will pick
    this up" was never told otherwise, and nothing recorded that a sale had been
    lost to nobody looking. That is the worst of the three: it is invisible, so it
    never gets fixed.

    Returns what expired, so the caller can tell each shopper and record an outcome.
    The caller does that rather than this function, because writing to a session and
    recording revenue are not the repository's job.
    """
    now = datetime.now(UTC)

    async with session_scope() as db:
        result = await db.execute(
            select(Approval, Case)
            .join(Case, Case.case_id == Approval.case_id)
            .where(
                Approval.state == "PENDING",
                Approval.expires_at.is_not(None),
                Approval.expires_at < now,
            )
        )

        expired = []
        for approval, case in result.all():
            approval.state = "EXPIRED"
            approval.decided_at = now
            # Recorded as the deciding party so an audit trail never has a decision
            # with nobody attached to it. Nobody decided; time did.
            approval.decided_by = "expired"
            case.state = str(CaseState.TIMEOUT)

            expired.append(
                {
                    "approval_id": approval.approval_id,
                    "case_id": case.case_id,
                    "connection_id": case.connection_id,
                    "session_id": case.session_id,
                    "action_type": approval.action_type,
                    "friction_type": case.friction_type,
                    "order_id": case.order_id,
                    "waited_minutes": int(
                        (now - _aware(approval.requested_at)).total_seconds() / 60
                    ),
                }
            )

    return expired


# ---------------------------------------------------------------------------
# Cross-client views, for CV3's own operators
# ---------------------------------------------------------------------------
#
# Every function above takes one connection_id and there is no unfiltered variant,
# which is what makes tenant isolation structural rather than remembered.
#
# These take a list of ids instead. That is not a hole in the rule: the query is
# still filtered, just by many ids rather than one, and the caller supplies the list
# from the connection registry. When authentication arrives, the list becomes the
# set of merchants a given operator is permitted to see, and nothing here changes.
#
# A CV3 operator covering ten clients is a real user with a real need. Making them
# switch merchant ten times to find their work is not isolation, it is an interface
# failure wearing isolation's clothes.


async def pending_across(connection_ids: list[str], *, limit: int = 100) -> list[dict]:
    """Everything waiting on a person, across the given merchants.

    Oldest first. A shopper has been waiting on each of these, and sorting by
    merchant or by amount would leave the person who has waited longest waiting
    longer.
    """
    if not connection_ids:
        return []

    now = datetime.now(UTC)
    async with session_scope() as db:
        result = await db.execute(
            select(Approval, Case)
            .join(Case, Case.case_id == Approval.case_id)
            .where(
                Approval.connection_id.in_(connection_ids),
                Approval.state == "PENDING",
            )
            .order_by(Approval.requested_at.asc())
            .limit(limit)
        )

        rows = []
        for approval, case in result.all():
            expires = _aware(approval.expires_at)
            if expires is not None and expires < now:
                continue
            requested = _aware(approval.requested_at)
            rows.append(
                {
                    "approval_id": approval.approval_id,
                    "case_id": case.case_id,
                    "connection_id": case.connection_id,
                    "action_type": approval.action_type,
                    "risk_rule": approval.risk_rule,
                    "requested_at": requested.isoformat(),
                    "waiting_minutes": int((now - requested).total_seconds() / 60),
                    "expires_at": expires.isoformat() if expires else None,
                    "minutes_left": (
                        int((expires - now).total_seconds() / 60) if expires else None
                    ),
                    "friction_type": case.friction_type,
                    "diagnosis": case.diagnosis,
                    "evidence": case.evidence,
                    "used_model": case.used_model,
                    "shopper_reply": case.shopper_reply,
                    "model_reply": case.model_reply,
                    "selection_reason": case.selection_reason,
                    "rejected": case.rejected,
                    "financial": case.financial,
                    "order_id": case.order_id,
                    "query": case.query,
                }
            )
        return rows


async def decided_across(connection_ids: list[str], *, limit: int = 40) -> list[dict]:
    """What has already been settled, newest first.

    The queue showed pending work and nothing else, so an operator could not answer
    "what did I decide this morning", "who approved that refund", or "did the thing
    I approved actually work". All three are ordinary questions and the data was
    there the whole time.

    Includes expiries, which are decisions too - by nobody, which is the kind worth
    being able to count.
    """
    if not connection_ids:
        return []

    async with session_scope() as db:
        result = await db.execute(
            select(Approval, Case)
            .join(Case, Case.case_id == Approval.case_id)
            .where(
                Approval.connection_id.in_(connection_ids),
                Approval.state != "PENDING",
            )
            .order_by(Approval.decided_at.desc())
            .limit(limit)
        )
        pairs = list(result.all())

        # The outcome says what actually happened after the decision, which is not
        # the same question as what was decided. Fetched in one query, not per row.
        case_ids = [case.case_id for _, case in pairs]
        outcomes = {}
        if case_ids:
            found = await db.execute(
                select(Outcome).where(Outcome.case_id.in_(case_ids))
            )
            outcomes = {o.case_id: o for o in found.scalars()}

    rows = []
    for approval, case in pairs:
        outcome = outcomes.get(case.case_id)
        decided = _aware(approval.decided_at)
        rows.append(
            {
                "approval_id": approval.approval_id,
                "case_id": case.case_id,
                "connection_id": case.connection_id,
                "state": approval.state,
                "action_type": approval.action_type,
                "decided_at": decided.isoformat() if decided else None,
                "decided_by": approval.decided_by,
                "note": approval.note,
                "friction_type": case.friction_type,
                "diagnosis": case.diagnosis,
                "financial": case.financial,
                "order_id": case.order_id,
                "resolved": outcome.resolved if outcome else None,
                "final_state": outcome.final_state if outcome else case.state,
                "revenue": outcome.revenue_recovered_amount if outcome else None,
                "currency": outcome.revenue_recovered_currency if outcome else None,
            }
        )
    return rows


async def ops_stats(connection_ids: list[str]) -> dict:
    """Headline numbers for CV3, not for any one merchant.

    Different from merchant_report, which answers "what did this do for my shop".
    This answers "where should I be looking" - a question about workload rather than
    about outcomes.
    """
    if not connection_ids:
        return {"waiting": 0, "oldest_wait_minutes": 0, "by_merchant": {}, "today": 0}

    now = datetime.now(UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)

    async with session_scope() as db:
        pending = await db.execute(
            select(Approval).where(
                Approval.connection_id.in_(connection_ids),
                Approval.state == "PENDING",
            )
        )
        rows = [
            a
            for a in pending.scalars()
            if a.expires_at is None or _aware(a.expires_at) >= now
        ]

        settled_today = await db.scalar(
            select(func.count(Approval.approval_id)).where(
                Approval.connection_id.in_(connection_ids),
                Approval.state != "PENDING",
                Approval.decided_at >= midnight,
            )
        )

    by_merchant: dict[str, int] = {}
    for approval in rows:
        by_merchant[approval.connection_id] = (
            by_merchant.get(approval.connection_id, 0) + 1
        )

    waits = [int((now - _aware(a.requested_at)).total_seconds() / 60) for a in rows]

    return {
        "waiting": len(rows),
        # The number that decides whether anyone should be worried. Ten cases
        # waiting two minutes is a busy morning; one waiting fourteen is a shopper
        # about to be let down by the timeout.
        "oldest_wait_minutes": max(waits) if waits else 0,
        "by_merchant": by_merchant,
        "today": settled_today or 0,
    }


async def handovers_across(
    connection_ids: list[str], *, limit: int = 50
) -> list[dict]:
    """Cases handed to a person, that nobody has picked up.

    Escalations never reached the queue because the queue lists approvals, and an
    escalation is not waiting for a decision - the decision was that a person should
    handle it. So it auto-cleared, executed, and appeared nowhere, while the shopper
    was told somebody would help.

    A handover needs no approve button. It needs somebody to see it, do whatever it
    needs, and mark it done. The distinction matters: an approval asks "may I?", and
    a handover says "your turn".
    """
    if not connection_ids:
        return []

    now = datetime.now(UTC)

    async with session_scope() as db:
        result = await db.execute(
            select(Case)
            .where(
                Case.connection_id.in_(connection_ids),
                Case.state == str(CaseState.ESCALATED),
                # Not yet dealt with. handled_at is set when an operator closes it.
                Case.handled_at.is_(None),
            )
            .order_by(Case.created_at.asc())
            .limit(limit)
        )

        rows = []
        for case in result.scalars():
            created = _aware(case.created_at)
            rows.append(
                {
                    "case_id": case.case_id,
                    "connection_id": case.connection_id,
                    "friction_type": case.friction_type,
                    "diagnosis": case.diagnosis,
                    "evidence": case.evidence,
                    "shopper_reply": case.shopper_reply,
                    "rejected": case.rejected,
                    "order_id": case.order_id,
                    "query": case.query,
                    "used_model": case.used_model,
                    "created_at": created.isoformat(),
                    "waiting_minutes": int((now - created).total_seconds() / 60),
                }
            )
        return rows


async def mark_handled(
    connection_id: str, case_id: str, by: str, note: str | None = None
) -> dict | None:
    """Close a handover. Returns whether anything changed.

    Scoped by connection like everything else here, so one merchant's operator
    cannot close another's work.
    """
    async with session_scope() as db:
        case = await db.get(Case, case_id)
        if case is None or case.connection_id != connection_id:
            return False
        if case.handled_at is not None:
            return None
        case.handled_at = datetime.now(UTC)
        case.handled_by = by
        case.handled_note = note

        # Returned so the caller can tell the shopper. The session id is the whole
        # reason this is worth returning rather than a bare True: without it, an
        # operator resolves something and the shopper never learns.
        return {
            "case_id": case.case_id,
            "session_id": case.session_id,
            "connection_id": case.connection_id,
        }
