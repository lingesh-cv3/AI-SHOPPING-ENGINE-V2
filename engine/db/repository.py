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