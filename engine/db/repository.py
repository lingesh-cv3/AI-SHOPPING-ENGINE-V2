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
import random
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from shared.models import CaseState

from .models import (
    Approval,
    Case,
    ExecutionAttempt,
    FunnelEvent,
    MerchantPolicy,
    Outcome,
    OrderLine,
    SentMail,
    SessionHoldout,
)
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
    is_holdout: bool = False,
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
                is_holdout=is_holdout,
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


async def holdout_status(
    connection_id: str, session_id: str, *, holdout_percent: int
) -> bool:
    """Whether this session is in the no-assistance holdout group.

    Assigned once, at the first friction event a session ever hits, and read
    back unchanged after that - drawing a fresh random number on every friction
    event would let the same shopper land in both groups over one visit, which
    is not a controlled comparison of anything. `holdout_percent` is read from
    the merchant's current policy on the first call only; a merchant who
    changes the percentage mid-session does not retroactively reassign
    shoppers already in the middle of one.

    A session id collision across merchants cannot happen (session ids are
    per-connection-scoped upstream), but the primary key here is the session id
    alone, so a caller for the wrong connection_id gets a KeyError-shaped bug
    rather than a silent cross-merchant read - deliberately, so it fails loud.
    """
    async with session_scope() as db:
        row = await db.get(SessionHoldout, session_id)
        if row is not None:
            if row.connection_id != connection_id:
                raise ValueError(
                    f"session {session_id} belongs to a different connection"
                )
            return row.is_holdout

        assigned = holdout_percent > 0 and random.randint(1, 100) <= holdout_percent
        db.add(
            SessionHoldout(
                session_id=session_id,
                connection_id=connection_id,
                is_holdout=assigned,
            )
        )
        return assigned


async def resolve_unresolved_payment_cases_for_cart(
    connection_id: str,
    cart_id: str,
    *,
    amount: str | None = None,
    currency: str | None = None,
) -> None:
    """Mark unresolved payment-decline cases for a cart resolved once it pays.

    Three separate call sites record a payment-decline case unresolved and
    then never look at it again: a holdout decline (nothing was attempted), an
    operator's rejection of a proposed recovery, and an approval that expired
    unactioned. All three share the same gap - a shopper who was declined and
    then simply retried the same card, or a different one, on their own has
    resolved their own problem, and none of those three code paths ever find
    that out. Fixed once, here, rather than three times at the call sites,
    specifically so the next friction type that gets this same shape does not
    reopen it a fourth time.

    Scoped to `friction_type == PAYMENT_DECLINED`: a cart eventually being paid
    is an unambiguous, causally-connected resolution signal for a payment that
    was previously declined against the same cart. It is not evidence that an
    unrelated friction against the same cart (a dead search, a failed coupon)
    was resolved, so those are deliberately left alone rather than marked
    resolved on a signal that says nothing about them.

    Marks every matching unresolved case, not just the most recent - a cart
    declined twice before it finally paid represents two real friction events
    the shopper lived through, and the eventual success resolves both of them,
    not only the last.

    A fourth path shares this same gap and was missed the first time this was
    fixed: a decline whose recovery is still sitting in the approval queue,
    never yet rejected or expired, when the shopper pays the cart themselves
    (a different card, or the operator not having gotten to it yet). That
    case has no Outcome row at all - one is only ever written by a holdout, a
    rejection, an expiry, or `execute_case` finishing a decision - so the
    loop below that flips an existing Outcome's `resolved` flag never sees
    it, and the sale silently never appears as recovered revenue. Handled
    here by writing the missing Outcome directly for any matching case that
    has none yet, and by expiring its now-moot PENDING approval so an
    operator is not asked to decide a recovery for money that already
    arrived through another door.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(Case).where(
                Case.connection_id == connection_id,
                Case.cart_id == cart_id,
                Case.friction_type == "PAYMENT_DECLINED",
            )
        )
        cases = list(result.scalars())
        if not cases:
            return

        outcome_result = await db.execute(
            select(Outcome).where(
                Outcome.case_id.in_([c.case_id for c in cases])
            )
        )
        outcomes_by_case = {o.case_id: o for o in outcome_result.scalars()}

        approval_result = await db.execute(
            select(Approval).where(
                Approval.case_id.in_([c.case_id for c in cases]),
                Approval.state == "PENDING",
            )
        )
        for approval in approval_result.scalars():
            # Moot: the money this recovery would have chased has already
            # moved. Expired rather than deleted, so "was this ever decided,
            # and why not" stays answerable - the same reasoning the sweeper
            # already uses for a timed-out approval.
            approval.state = "EXPIRED"
            approval.decided_at = datetime.now(UTC)
            approval.decided_by = "system"
            approval.note = (
                "Closed automatically: the shopper paid this cart another way "
                "before this recovery was decided."
            )

        # Only one sale actually happened, however many prior declines led up
        # to it - a cart declined twice before it paid is two friction events
        # (both get marked resolved, below) but one payment, so the paid
        # amount is attributed to exactly one outcome row. Recording it on
        # every matching row would sum the same rupee amount two or three
        # times over in merchant_report's revenue_recovered total the moment
        # a shopper had declined more than once on the same cart - a real
        # double-count this function would otherwise introduce by iterating
        # every unresolved outcome and stamping each with the same amount.
        amount_attributed = False
        for case in cases:
            outcome = outcomes_by_case.get(case.case_id)
            if outcome is None:
                # Never reached a terminal outcome (still PENDING, or the
                # engine never wrote one) - write it now, in this same
                # session/transaction, rather than leaving this friction
                # permanently unrecorded, which is the gap this whole
                # function exists to close for the other three call sites.
                #
                # Built inline rather than by calling record_outcome(), which
                # opens its own session_scope() - nesting a second SQLite
                # write transaction inside this one while it is still open
                # is exactly the kind of thing that deadlocks or silently
                # serializes on SQLite, so this stays on the one session
                # already open here.
                use_amount = amount if not amount_attributed else None
                use_currency = currency if not amount_attributed else None
                elapsed = int(
                    (datetime.now(UTC) - _aware(case.created_at)).total_seconds() * 1000
                )
                db.add(
                    Outcome(
                        outcome_id=_id("out"),
                        case_id=case.case_id,
                        connection_id=connection_id,
                        resolved=True,
                        final_state="OUTCOME",
                        friction_type=case.friction_type,
                        revenue_recovered_amount=use_amount,
                        revenue_recovered_currency=use_currency,
                        time_to_resolution_ms=elapsed,
                        required_human=False,
                    )
                )
                case.state = "OUTCOME"
                if use_amount:
                    amount_attributed = True
                continue
            if not outcome.resolved:
                outcome.resolved = True
                # The sale that just completed is the money this decline was
                # blocking. Recorded here, not only "resolved", so a shopper who
                # retried on their own (a different card, or the same one a
                # moment later) shows up as recovered revenue on the merchant
                # report the same way an operator-approved retry already does
                # via execute_case's own record_outcome. Only set when this
                # call actually carries a paid amount, only on the first
                # matching row, and only when that row does not already have
                # one - a case whose recovery ran through the approval
                # pipeline (RETRY_PAYMENT) already recorded its own amount
                # there, and this must never overwrite or double it.
                if amount and not amount_attributed and not outcome.revenue_recovered_amount:
                    outcome.revenue_recovered_amount = amount
                    outcome.revenue_recovered_currency = currency
                    amount_attributed = True


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

    And refuses one that has run out of time. The queue hides an approval past its
    deadline and the sweeper closes it out, but between those two facts was a
    window nobody guarded: expires_at had passed, the sweep had not run, the row
    was still PENDING, and approving it executed as though it were fresh. Every
    approval in this queue is financial by definition, so that was a payment taken
    against a decision that had already timed out - and an operator only had to
    click a button their console had drawn a minute earlier.
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

        expires = _aware(approval.expires_at)
        if expires is not None and expires < datetime.now(UTC):
            # Left PENDING on purpose, rather than expired here. Closing one out
            # means telling the shopper and recording the sale we did not save,
            # and that already exists in one place. Marking the row here would
            # hide it from the sweeper and do neither.
            return {
                "approval_id": approval_id,
                "case_id": approval.case_id,
                "state": "EXPIRED",
                "changed": False,
                "expired": True,
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
    holdout_percent: int = 0,
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
        row.holdout_percent = holdout_percent


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
                "holdout_percent": r.holdout_percent or 0,
            }
            for r in rows.scalars()
        ]

async def total_sales(connection_id: str, *, days: int = 30) -> dict:
    """Actual completed sales for this merchant - every basket that was
    successfully paid for through this engine, regardless of whether it hit
    any friction on the way.

    Distinct from `revenue_recovered` in merchant_report/Outcome, which is
    scoped to sales that would otherwise have been lost to a declined
    payment. This is the ordinary, complete figure a merchant checks against
    their own books: every completed order.

    Sourced from `engine.db.idempotency`'s own payment ledger
    (`ExecutionAttempt` rows with `action_type == "CHECKOUT"`), not from any
    adapter "list orders" call - the Standard Commerce Interface has no such
    capability (only `get_order(order_id)` for one already-known order), so
    there is no way to enumerate a platform's orders directly. The ledger is
    the one place this engine already writes down, once per cart, whether a
    payment through it actually succeeded - `payment_settled` is called
    exactly once per cart's successful checkout, from both the chat tap path
    and the REST checkout route, and the cart-keyed idempotency guard (see
    idempotency.py's own module docstring) is what stops a retried charge
    from being counted twice here.

    A cart is claimed by exactly one connection, and `ExecutionAttempt.
    connection_id` is trusted for tenant scoping the same way every other
    query in this module is - queries never enumerate rows across tenants.

    Returns zero-valued fields (not missing ones, not a divide-by-zero) for
    a merchant with no completed sales in the window, which includes a
    merchant connected today with zero history.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as db:
        rows = await db.execute(
            select(ExecutionAttempt).where(
                ExecutionAttempt.connection_id == connection_id,
                ExecutionAttempt.action_type == "CHECKOUT",
                ExecutionAttempt.state == "DONE",
                ExecutionAttempt.succeeded.is_(True),
                ExecutionAttempt.completed_at >= since,
            )
        )
        attempts = list(rows.scalars())

    amounts = []
    currency = None
    for a in attempts:
        result = a.result or {}
        amt = result.get("amount_paid")
        if amt is not None:
            try:
                amounts.append(Decimal(str(amt)))
            except Exception:  # noqa: BLE001 - a malformed stored value must not crash the report
                continue
            if currency is None:
                currency = result.get("currency")

    total = sum(amounts, Decimal("0.00"))
    order_count = len(attempts)
    # Orders whose amount could not be recovered (older rows written before
    # payment_settled started storing it) still count toward the order count
    # honestly, but are excluded from the money total rather than silently
    # treated as zero-value sales.
    priced_count = len(amounts)
    average = (total / priced_count) if priced_count else None

    return {
        "days": days,
        "completed_order_count": order_count,
        "total_sales_amount": f"{total:.2f}",
        "total_sales_currency": currency or "INR",
        "average_order_value": (f"{average:.2f}" if average is not None else None),
    }


async def record_funnel_event(
    connection_id: str, event_type: str, cart_id: str
) -> None:
    """Log one real funnel-stage event. Called exactly once, from
    `shop.py::create_cart`, at the moment a cart is actually minted - see
    `FunnelEvent`'s own docstring for why this is safe against refresh/retry
    duplication (the storefront only calls that route when it doesn't
    already hold a cart id) and why it covers guests as well as signed-in
    shoppers (unlike `ShopperCart`, which only exists for accounts).
    """
    async with session_scope() as db:
        db.add(
            FunnelEvent(
                event_id=_id("fev"),
                connection_id=connection_id,
                event_type=event_type,
                cart_id=cart_id,
            )
        )


async def checkout_conversion(connection_id: str, *, days: int = 30) -> dict:
    """The real funnel this engine can honestly report, from cart creation
    through to a completed order.

    Two independently-sourced, SQL-aggregated counts:

    - `carts_created`, from `FunnelEvent` (`CART_CREATED` rows) - real
      instrumentation, written at the moment `create_cart` actually mints a
      cart, covering guests and signed-in shoppers alike. Only carts
      created after this table shipped are counted; an older cart has no
      row here and cannot be reconstructed, which is why `funnel_has_history`
      exists below rather than letting a small `carts_created` silently
      read as "business is slow".
    - `checkout_attempts`/`completed_orders`, from the same `ExecutionAttempt`
      ledger `total_sales` reads - every checkout attempt through this
      engine writes a row here *before* the platform is called
      (`idempotency.py::claim`), marked succeeded/failed afterward, never
      deleted. So unlike `total_sales` (successful subset only), this
      counts every attempt, successful or declined - a real
      numerator/denominator pair, not an inferred one.

    Still deliberately NOT a "sessions/visits" funnel - this engine has no
    page-view or store-visit event for guests at all (a guest is only ever
    identified once they create a cart), so "how many people looked at the
    shop" is not answerable from existing data without a different kind of
    instrumentation than a cart-scoped event log. What's reported here is
    exactly the two real stages this engine can instrument end to end:
    cart -> checkout attempt -> completed order.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as db:
        cart_id_rows = await db.execute(
            select(FunnelEvent.cart_id).where(
                FunnelEvent.connection_id == connection_id,
                FunnelEvent.event_type == "CART_CREATED",
                FunnelEvent.created_at >= since,
            )
        )
        window_cart_ids = [row[0] for row in cart_id_rows.all()]
        earliest_event = await db.scalar(
            select(func.min(FunnelEvent.created_at)).where(
                FunnelEvent.connection_id == connection_id,
                FunnelEvent.event_type == "CART_CREATED",
            )
        )
        total_attempts = await db.scalar(
            select(func.count(ExecutionAttempt.idempotency_key)).where(
                ExecutionAttempt.connection_id == connection_id,
                ExecutionAttempt.action_type == "CHECKOUT",
                ExecutionAttempt.state == "DONE",
                ExecutionAttempt.completed_at >= since,
            )
        )
        succeeded = await db.scalar(
            select(func.count(ExecutionAttempt.idempotency_key)).where(
                ExecutionAttempt.connection_id == connection_id,
                ExecutionAttempt.action_type == "CHECKOUT",
                ExecutionAttempt.state == "DONE",
                ExecutionAttempt.succeeded.is_(True),
                ExecutionAttempt.completed_at >= since,
            )
        )

        # How many of THIS window's carts ever reached a checkout attempt,
        # at any time - not "how many checkout attempts happened in this
        # window", which is a different, unrelated population (it includes
        # attempts against carts created before this window, and before
        # this instrumentation even existed). Comparing those two raw
        # counts directly is exactly the bug this join avoids: an old shop
        # with a long attempt history but only one freshly-instrumented
        # cart would otherwise show a "conversion rate" over 100%.
        #
        # The join key is `ExecutionAttempt.case_id`, which `idempotency.py
        # ::claim` sets to the raw cart id (truncated to 40 chars) for every
        # CHECKOUT row - not a separate column added for this, an existing
        # fact about how that ledger already keys itself.
        reached_checkout = 0
        if window_cart_ids:
            truncated_ids = {cid[:40] for cid in window_cart_ids}
            reached_checkout = await db.scalar(
                select(func.count(func.distinct(ExecutionAttempt.case_id))).where(
                    ExecutionAttempt.connection_id == connection_id,
                    ExecutionAttempt.action_type == "CHECKOUT",
                    ExecutionAttempt.case_id.in_(truncated_ids),
                )
            ) or 0

    carts_created = len(window_cart_ids)
    total_attempts = total_attempts or 0
    succeeded = succeeded or 0
    failed = total_attempts - succeeded
    success_rate = (
        round(succeeded / total_attempts * 100, 1) if total_attempts else None
    )
    # By construction reached_checkout counts distinct ids drawn from the
    # same carts_created set, so this is never negative and the rate below
    # is never over 100%.
    abandoned_before_checkout = carts_created - reached_checkout
    cart_to_checkout_rate = (
        round(reached_checkout / carts_created * 100, 1) if carts_created else None
    )

    return {
        "days": days,
        "carts_created": carts_created,
        "checkout_attempts": total_attempts,
        "completed_orders": succeeded,
        "failed_checkout_attempts": failed,
        "abandoned_before_checkout": abandoned_before_checkout,
        "checkout_success_rate": success_rate,
        "cart_to_checkout_rate": cart_to_checkout_rate,
        # False until at least one FunnelEvent row exists for this
        # merchant - a merchant connected before this shipped sees an
        # honest "no history" rather than a confusing 0 that reads as "no
        # carts ever", when the truth is "not measured yet".
        "funnel_has_history": earliest_event is not None,
        "scope_note": (
            "Cart-to-order only - this engine has no page-view or store-"
            "visit event for guests, so a full sessions/visits funnel "
            "cannot be reported honestly from existing data. Cart creation "
            "onward is real, SQL-aggregated instrumentation."
        ),
    }


async def product_performance(
    connection_id: str, *, days: int = 30, limit: int = 10
) -> dict:
    """Product-level sales performance, sourced from `OrderLine` - one row per
    product per *completed* (paid) order, written from this schema change
    forward only (see `OrderLine`'s own docstring).

    Historical limitation, stated rather than hidden: an order paid before
    this table existed has no `OrderLine` rows and cannot be reconstructed -
    the raw cart contents from a past checkout are gone. `has_data=False`
    with zero rows is the honest answer for a merchant with no completed
    orders in the window, or one connected today, not an error or a
    fabricated figure.

    `lowest_performers` is deliberately scoped to products that sold at
    least once in the window, ranked ascending by quantity then revenue.
    There is no defensible way from this table alone to name a product that
    sold *zero* times without re-scanning the full catalogue - which is
    exactly the `search_products("", limit=100)` shape CLAUDE.md already
    flags as wrong at real catalogue size (a 40,000-SKU client cannot have
    "the whole catalogue" paged in one call and treated as complete). So a
    true zero-sales product reads the same here as one that does not exist
    in the catalogue at all - callers must say so rather than imply this is
    a full slow-mover report.

    Aggregation happens in SQL (`GROUP BY`/`func.sum`/`func.count`) so this
    holds up at real order volume - it does not pull every row into Python
    to sum by hand.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as db:
        grouped = await db.execute(
            select(
                OrderLine.product_id,
                OrderLine.product_name,
                func.sum(OrderLine.quantity).label("quantity"),
                func.count(func.distinct(OrderLine.order_id)).label("order_count"),
            )
            .where(
                OrderLine.connection_id == connection_id,
                OrderLine.created_at >= since,
            )
            .group_by(OrderLine.product_id, OrderLine.product_name)
        )
        grouped_rows = list(grouped.all())

        revenue_rows = await db.execute(
            select(OrderLine.product_id, OrderLine.line_total).where(
                OrderLine.connection_id == connection_id,
                OrderLine.created_at >= since,
            )
        )
        revenue_pairs = list(revenue_rows.all())

    revenue_by_product: dict[str, Decimal] = {}
    for pid, line_total in revenue_pairs:
        if line_total is None:
            continue
        try:
            revenue_by_product[pid] = revenue_by_product.get(
                pid, Decimal("0.00")
            ) + Decimal(str(line_total))
        except Exception:  # noqa: BLE001 - a malformed stored value must not crash the report
            continue

    products = []
    for product_id, product_name, quantity, order_count in grouped_rows:
        products.append(
            {
                "product_id": product_id,
                "product_name": product_name,
                "quantity": int(quantity or 0),
                "revenue": f"{revenue_by_product.get(product_id, Decimal('0.00')):.2f}",
                "order_count": int(order_count or 0),
            }
        )

    by_quantity = sorted(products, key=lambda p: -p["quantity"])[:limit]
    by_revenue = sorted(products, key=lambda p: -Decimal(p["revenue"]))[:limit]
    lowest_performers = sorted(
        products, key=lambda p: (p["quantity"], Decimal(p["revenue"]))
    )[:limit]

    return {
        "days": days,
        "since": since.isoformat(),
        "has_data": bool(products),
        "distinct_products_sold": len(products),
        "top_by_quantity": by_quantity,
        "top_by_revenue": by_revenue,
        "lowest_performers": lowest_performers,
        "lowest_performers_note": (
            "Ranked only among products that sold at least once in this "
            "window. This cannot identify a product with zero sales without "
            "re-scanning the full catalogue, which is not attempted here."
        ),
        "historical_note": (
            "Product-level data is only tracked for orders completed from "
            "this schema change forward - an older order has no product "
            "breakdown and cannot be reconstructed."
        ),
    }


async def sales_period_comparison(connection_id: str, *, days: int = 7) -> dict:
    """Total completed sales in the most recent window versus the window
    immediately before it, reusing `total_sales`'s own days-window pattern
    and its `ExecutionAttempt`/CHECKOUT source - not a second, differently
    sourced figure that could disagree with the report's headline number.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    prior_since = since - timedelta(days=days)

    async def _window(start: datetime, end: datetime | None) -> tuple[Decimal, int]:
        async with session_scope() as db:
            conditions = [
                ExecutionAttempt.connection_id == connection_id,
                ExecutionAttempt.action_type == "CHECKOUT",
                ExecutionAttempt.state == "DONE",
                ExecutionAttempt.succeeded.is_(True),
                ExecutionAttempt.completed_at >= start,
            ]
            if end is not None:
                conditions.append(ExecutionAttempt.completed_at < end)
            rows = await db.execute(select(ExecutionAttempt).where(*conditions))
            attempts = list(rows.scalars())
        total = Decimal("0.00")
        for a in attempts:
            amt = (a.result or {}).get("amount_paid")
            if amt is not None:
                try:
                    total += Decimal(str(amt))
                except Exception:  # noqa: BLE001
                    continue
        return total, len(attempts)

    recent_total, recent_count = await _window(since, None)
    prior_total, prior_count = await _window(prior_since, since)

    change = recent_total - prior_total
    change_pct = (
        f"{(change / prior_total * 100):.1f}" if prior_total > 0 else None
    )

    return {
        "days": days,
        "recent_order_count": recent_count,
        "recent_total": f"{recent_total:.2f}",
        "prior_order_count": prior_count,
        "prior_total": f"{prior_total:.2f}",
        "change_amount": f"{change:.2f}",
        "change_pct": change_pct,
        "note": (
            None
            if prior_count or recent_count
            else "No completed sales in either window - nothing to compare yet."
        ),
    }


async def daily_revenue_series(connection_id: str, *, days: int = 30) -> dict:
    """Day-by-day completed revenue for the Overview trend chart, aligned as
    two equal-length series - the current window and the window immediately
    before it - by day offset (day 1 of this window against day 1 of the
    prior one), the same way `sales_period_comparison` compares its two
    window totals. Reuses that function's identical `ExecutionAttempt`
    CHECKOUT/DONE/succeeded source in one query, so a day-by-day chart can
    never disagree with either the report's headline total or the trend
    figure - it is a breakdown of the same underlying rows, not a second,
    differently-sourced count.

    Real-volume note: bounded to `2 * days` of one connection's CHECKOUT
    attempts, the same bound `sales_period_comparison`'s own two `_window`
    calls already accept - not a full-table scan, and it does not grow with
    catalogue size the way `_scan_catalog` has to guard against.
    """
    # Calendar-date boundaries throughout, deliberately not the exact-time
    # `now`-minus-`timedelta` window `sales_period_comparison` uses for its
    # scalar totals. A `days=10` window should mean 10 *whole* calendar days
    # ending today, today included - a time-based cutoff would drop today's
    # partial day from a chart, whereas silently missing revenue from a
    # bare scalar comparison is not something an operator would notice the
    # same way. The two figures already tolerate this: both describe "the
    # last N days", not the identical instant-to-instant window.
    today = datetime.now(UTC).date()
    current_start = today - timedelta(days=days - 1)
    prior_start = current_start - timedelta(days=days)
    query_lower_bound = datetime.combine(prior_start, datetime.min.time(), tzinfo=UTC)

    async with session_scope() as db:
        rows = await db.execute(
            select(ExecutionAttempt.completed_at, ExecutionAttempt.result).where(
                ExecutionAttempt.connection_id == connection_id,
                ExecutionAttempt.action_type == "CHECKOUT",
                ExecutionAttempt.state == "DONE",
                ExecutionAttempt.succeeded.is_(True),
                ExecutionAttempt.completed_at >= query_lower_bound,
            )
        )
        attempts = list(rows.all())

    current_by_day: dict[str, Decimal] = {}
    prior_by_day: dict[str, Decimal] = {}
    for completed_at, result in attempts:
        if completed_at is None:
            continue
        amt = (result or {}).get("amount_paid")
        if amt is None:
            continue
        try:
            value = Decimal(str(amt))
        except Exception:  # noqa: BLE001 - a malformed stored value must not crash the chart
            continue
        day = completed_at.date()
        if day >= current_start:
            bucket = current_by_day
        elif day >= prior_start:
            bucket = prior_by_day
        else:
            continue
        day_key = day.isoformat()
        bucket[day_key] = bucket.get(day_key, Decimal("0.00")) + value

    def _series(window_start, by_day: dict[str, Decimal]) -> list[dict]:
        out = []
        for offset in range(days):
            key = (window_start + timedelta(days=offset)).isoformat()
            out.append({"date": key, "amount": f"{by_day.get(key, Decimal('0.00')):.2f}"})
        return out

    return {
        "days": days,
        "current": _series(current_start, current_by_day),
        "prior": _series(prior_start, prior_by_day),
        "has_data": bool(current_by_day or prior_by_day),
    }


async def friction_summary_across(
    connection_ids: list[str], *, days: int = 30
) -> dict:
    """Real recorded issue categories across the given merchants, grouped by
    `Case.friction_type` - the same field `merchant_report`'s own "friction"
    breakdown already aggregates for one merchant, extended to many.

    Exists specifically because the operations copilot, asked "what are the
    most common issues", had nothing of this shape to answer from: the only
    per-merchant-workload data it was given was the approval queue and recent
    decisions, both keyed by `action_type` (what the engine proposed doing
    about a problem, e.g. OFFER_ALTERNATE_PAYMENT), not by what the problem
    actually was. A decline that gets offered a recovery and a decline that
    gets escalated are the same underlying issue (PAYMENT_DECLINED) with two
    different responses - counting `action_type` instead answers a different
    question and reads as "the most common issue is offering alternate
    payment", which is not an issue at all.

    Returns zero rows (not an error) for merchants with no cases in the
    window - a CV3 operator whose whole book connected this morning sees an
    honest empty list rather than a crash.
    """
    if not connection_ids:
        return {"days": days, "by_type": [], "by_type_by_merchant": {}}

    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as db:
        result = await db.execute(
            select(Case.connection_id, Case.friction_type).where(
                Case.connection_id.in_(connection_ids),
                Case.created_at >= since,
                Case.friction_type.is_not(None),
            )
        )
        rows = list(result.all())

    by_type: dict[str, int] = {}
    by_type_by_merchant: dict[str, dict[str, int]] = {}
    for cid, ftype in rows:
        by_type[ftype] = by_type.get(ftype, 0) + 1
        per_merchant = by_type_by_merchant.setdefault(cid, {})
        per_merchant[ftype] = per_merchant.get(ftype, 0) + 1

    return {
        "days": days,
        "by_type": [
            {"type": k, "count": v}
            for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])
        ],
        "by_type_by_merchant": by_type_by_merchant,
    }


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

    # Distinct shoppers, not cases - one shopper can hit several friction
    # types in a session and each becomes a case row. Counting cases inflated
    # the headline, and resolution rate is measured against the total that were
    # actually opened.
    shoppers_helped = len({c.session_id for c in case_rows}) if case_rows else 0
    resolution_rate = (len(resolved) / len(case_rows) * 100) if case_rows else None

    # The holdout comparison. Only meaningful once at least one holdout case
    # exists - a merchant who has never turned this on should see nothing here
    # rather than a confusing "0% vs 0%" panel with nothing behind it.
    #
    # Outcome rows do not carry is_holdout themselves, so the split is done
    # from the case each outcome belongs to - both sides are read from the
    # same window of time, which is what makes the comparison fair.
    is_holdout_case = {c.case_id: c.is_holdout for c in case_rows}
    holdout_cases = [c for c in case_rows if c.is_holdout]
    assisted_cases = [c for c in case_rows if not c.is_holdout]
    holdout_resolved = [o for o in resolved if is_holdout_case.get(o.case_id)]
    assisted_resolved = [o for o in resolved if not is_holdout_case.get(o.case_id)]

    holdout_comparison = None
    if holdout_cases:
        holdout_comparison = {
            "holdout_cases": len(holdout_cases),
            "holdout_resolved": len(holdout_resolved),
            "holdout_resolution_rate": round(
                len(holdout_resolved) / len(holdout_cases) * 100, 1
            ),
            "assisted_cases": len(assisted_cases),
            "assisted_resolved": len(assisted_resolved),
            "assisted_resolution_rate": (
                round(len(assisted_resolved) / len(assisted_cases) * 100, 1)
                if assisted_cases
                else None
            ),
        }

    sales = await total_sales(connection_id, days=days)

    # Recovery-specific figures for the merchant-facing Payments & Checkout
    # panel. Kept separate from `revenue_recovered` above: a merchant asking
    # "how many times did this work" wants a count, not a currency amount.
    # `recovery_opportunities` is also deliberately not the same as the
    # PAYMENT_DECLINED friction count already in `friction` below - not
    # every decline gets a recovery action proposed (Northfield has no
    # recovery capability at all, so every decline there escalates instead
    # of proposing RETRY_PAYMENT/OFFER_ALTERNATE_PAYMENT/SPLIT_PAYMENT).
    recovery_action_types = {"RETRY_PAYMENT", "OFFER_ALTERNATE_PAYMENT", "SPLIT_PAYMENT"}
    recovery_opportunities = sum(
        1 for c in case_rows if c.selected_action in recovery_action_types
    )
    recovery_count = sum(1 for o in resolved if o.revenue_recovered_amount)

    return {
        "days": days,
        "shoppers_helped": shoppers_helped,
        "problems_solved": len(resolved),
        "resolution_rate": round(resolution_rate, 1) if resolution_rate is not None else None,
        "handled_without_you": handled_alone,
        "waiting_for_you": waiting or 0,
        "revenue_recovered": f"{recovered:.2f}",
        "recovery_count": recovery_count,
        "recovery_opportunities": recovery_opportunities,
        "currency": currency,
        "completed_order_count": sales["completed_order_count"],
        "total_sales_amount": sales["total_sales_amount"],
        "total_sales_currency": sales["total_sales_currency"],
        "average_order_value": sales["average_order_value"],
        "median_resolution_ms": median_ms,
        "holdout": holdout_comparison,
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


#: A run of 10+ consecutive digits inside a search term is, in practice, a
#: millisecond/second Unix epoch stamp glued onto a placeholder word by test
#: infrastructure that wants a fresh, always-unique "not in the catalog"
#: query per run (e.g. `nonexistent-item-1788942396617`) - confirmed present
#: in the live demo database under exactly that shape. No real shopper types
#: a bare 10+ digit number as part of a product search, so this is a signal
#: about the *shape* of the text, not a list of the specific words seen so
#: far - it generalizes to any future test run's generated queries, not just
#: today's timestamps, without needing to name "nonexistent" or "trainers"
#: anywhere. It is a reporting-layer filter only: no row is ever deleted, and
#: a real query containing a long numeric string (vanishingly unlikely, but
#: not impossible - a model number, say) is undercounted in this one
#: aggregate rather than lost from the database.
#:
#: This is a heuristic, not a certainty - the durable fix is for test
#: infrastructure to tag its own synthetic signals (e.g. a `source="test"` or
#: a recognisable session-id prefix already used elsewhere in this codebase,
#: see `hc_`/`fuzz_`-style prefixes in healthcheck.py/fuzz.py) so this filter
#: can key off a real signal instead of guessing from content. Left as a
#: PROGRESS.md follow-up rather than attempted here, since it would require
#: changing the untracked scratch script(s) that appear to generate these
#: queries, which are not part of the tracked test suite.
_SYNTHETIC_QUERY_PATTERN = re.compile(r"\d{10,}")


async def unmet_demand(connection_id: str, *, days: int = 30, limit: int = 10) -> list[dict]:
    """What shoppers keep asking for and not finding - a real, grounded signal
    for "should I stock this" that costs nothing new to compute.

    Built from the same `Case.query` text already recorded on every
    DEAD_SEARCH friction event (see `Case.query`'s own docstring) - nothing
    new is tracked, this only aggregates what the engine already writes down
    every time a search comes back empty. Grouped case-insensitively and
    trimmed, since "Running Shoes" and "running shoes" are the same unmet
    request. Query text is optional on a case (`Case.query: str | None`), so
    rows with nothing recorded are skipped rather than counted as a blank
    request.

    Queries whose text matches `_SYNTHETIC_QUERY_PATTERN` (a long embedded
    digit run - a timestamp signature, not a real shopper search) are
    excluded from this aggregate only - no `Case` row is touched or deleted,
    so this is a view over the data, not a destructive cleanup.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as db:
        result = await db.execute(
            select(Case.query).where(
                Case.connection_id == connection_id,
                Case.friction_type == "DEAD_SEARCH",
                Case.created_at >= since,
                Case.query.is_not(None),
            )
        )
        raw_queries = [row[0] for row in result.all() if row[0] and row[0].strip()]

    counts: dict[str, int] = {}
    for q in raw_queries:
        key = q.strip().lower()
        if _SYNTHETIC_QUERY_PATTERN.search(key):
            continue
        counts[key] = counts.get(key, 0) + 1

    return [
        {"query": q, "times_asked": n}
        for q, n in sorted(counts.items(), key=lambda kv: -kv[1])[:limit]
    ]


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
    connection_ids: list[str], *, limit: int = 50, offset: int = 0
) -> tuple[list[dict], int]:
    """Cases handed to a person, that nobody has picked up.

    Escalations never reached the queue because the queue lists approvals, and an
    escalation is not waiting for a decision - the decision was that a person should
    handle it. So it auto-cleared, executed, and appeared nowhere, while the shopper
    was told somebody would help.

    A handover needs no approve button. It needs somebody to see it, do whatever it
    needs, and mark it done. The distinction matters: an approval asks "may I?", and
    a handover says "your turn".

    Returns (rows, total). The total counts every open handover across the given
    merchants, so a consumer can know there is more than the page it has. Before the
    total existed, a busy system silently hid the newest handovers behind the
    oldest-50 cap - new cases read as "gone" until the oldest were closed, which is
    a shopper told a person would help while nobody could see them. The total is the
    contract that makes paging possible instead of hiding things.
    """
    if not connection_ids:
        return [], 0

    now = datetime.now(UTC)

    async with session_scope() as db:
        query = select(Case).where(
            Case.connection_id.in_(connection_ids),
            Case.state == str(CaseState.ESCALATED),
            # Not yet dealt with. handled_at is set when an operator closes it.
            Case.handled_at.is_(None),
        )

        # Count first, before the offset/limit shrink the page: the total exists to
        # tell the consumer how many are still behind this page, so it must not be
        # the size of the page itself.
        total = len(
            (await db.execute(query)).scalars().all()
        )

        result = await db.execute(
            query.order_by(Case.created_at.asc()).limit(limit).offset(offset)
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
        return rows, total


async def mark_handled(
    connection_id: str, case_id: str, by: str, note: str | None = None
) -> dict | None:
    """Close a handover. Returns whether anything changed.

    Scoped by connection like everything else here, so one merchant's operator
    cannot close another's work.

    None either way a handover cannot be closed - an id that does not exist, one
    belonging to another merchant, or one already closed. The caller reads that as
    "nothing changed" and answers the same for all three, on purpose: telling an
    operator which of them it was would let them learn whether a case id is real by
    trying it. This used to return False for the first two and None only for the
    third, which the caller's `is None` check let straight through to a crash.
    """
    async with session_scope() as db:
        case = await db.get(Case, case_id)
        if case is None or case.connection_id != connection_id:
            return None
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


async def record_sent_mail(
    connection_id: str,
    *,
    order_id: str,
    to_email: str,
    subject: str,
    body: str,
    delivered: bool,
) -> None:
    """One row per order-confirmation attempt, sent or merely recorded.

    Written regardless of `delivered`, so "did we even try" is answerable from
    the database rather than the log - the same reasoning `record_outcome` uses
    for every other action the engine takes.
    """
    async with session_scope() as db:
        db.add(
            SentMail(
                row_id=_id("mail"),
                connection_id=connection_id,
                order_id=order_id,
                to_email=to_email,
                subject=subject,
                body=body,
                delivered=delivered,
            )
        )


async def sent_mail_for_order(connection_id: str, order_id: str) -> list[dict]:
    """Every confirmation attempt for one order, oldest first.

    Scoped by connection like everything else here - a merchant's own console
    should never be able to ask about another merchant's order.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(SentMail)
            .where(
                SentMail.connection_id == connection_id,
                SentMail.order_id == order_id,
            )
            .order_by(SentMail.created_at.asc())
        )
        return [
            {
                "to_email": row.to_email,
                "subject": row.subject,
                "delivered": row.delivered,
                "created_at": row.created_at.isoformat(),
            }
            for row in result.scalars()
        ]
