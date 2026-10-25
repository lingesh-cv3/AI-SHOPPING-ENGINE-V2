"""Purchase outcome + post-purchase, via the EXISTING `CHECK_ORDER_STATUS`
dispatch (`engine/execution/service.py`, unmodified) - same
decision->risk->record_case->execute_case shape `checkout.py` already
uses (non-financial, auto-approved, completes in one turn).

Ownership is the one extra check this module must reproduce exactly as
V1's `engine/api/chat.py` already does it (unmodified there too): the
execution dispatch itself has no shopper identity to check an order
against (an order id is guessable), so line items are only ever disclosed
after an explicit `db.owners.owner_of(..., ORDER, order_id)` check against
the caller's own keys - the base status (paid/not, total) is safe to state
regardless, exactly mirroring chat.py's own comment on this.
"""

from __future__ import annotations

import logging

from engine import db
from engine.db import record_case
from engine.decision import DecisionEngine, operation_for
from engine.risk import RiskGate
from engine.execution import ExecutionService
from engine.session import store as session_store
from shared.models import ActionType, ProposedAction

logger = logging.getLogger(__name__)

_NO_ORDER_REPLY = (
    "I don't have an order number to look up - tell me the order number, "
    "or ask right after checking out and I'll use the one from this visit."
)


async def run_order_status_turn(
    *,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    message: str,
    hinted_order_id: str | None,
    owner_keys: list[str] | None,
) -> tuple[str, dict | None]:
    """Returns (reply, payload_or_None). Never infers purchase success from
    anything but the real, authoritative order the adapter returns - never
    from an attempted payment, never from conversation memory."""
    # An id the shopper named wins. Otherwise "my order" is the newest order THIS caller holds (their account, else their browser) - that covers a
    # purchase made from the cart panel, a new device and a new conversation - and only then the newest order this conversation itself recorded.
    order_id = hinted_order_id
    if not order_id and owner_keys:
        order_id = await db.owners.latest_owned(connection_id, db.owners.ORDER, owner_keys)
    if not order_id:
        order_id = await session_store.latest_order(session_id, connection_id)
    if not order_id:
        return (_NO_ORDER_REPLY, None)

    proposed = ProposedAction(
        action_type=ActionType.CHECK_ORDER_STATUS,
        operation=operation_for(ActionType.CHECK_ORDER_STATUS),
        parameters={"order_id": order_id, "said": message[:80]},
        rationale="shopper asked about an order",
        confidence=0.8,
    )

    trace = await decision.decide([proposed], connection_id=connection_id)
    risk_decision = gate.classify(trace.selected)

    case_id = None
    try:
        case_id = await record_case(
            connection_id=connection_id,
            friction=None,
            query=message[:300],
            cart_id=None,
            order_id=order_id,
            session_id=session_id,
            reasoning={
                "used_model": False, "model_name": None, "diagnosis": None,
                "evidence": [], "fallback_reason": None, "model_reply": None,
                "shopper_reply": None, "prompt_tokens": None, "completion_tokens": None,
            },
            decision={
                "proposed": [
                    {
                        "action_type": str(proposed.action_type),
                        "parameters": proposed.parameters,
                        "rationale": proposed.rationale,
                        "confidence": proposed.confidence,
                    }
                ],
                "rejected": [
                    {"action_type": str(r.action_type), "reason": r.reason, "detail": r.detail}
                    for r in trace.rejected
                ],
                "selected_action": str(trace.selected.action.action_type),
                "selection_reason": trace.selected.selection_reason,
            },
            risk={
                "outcome": str(risk_decision.outcome),
                "rule": risk_decision.policy_rule,
                "reason": risk_decision.reason,
                "financial": risk_decision.properties.financial,
            },
        )
    except Exception:  # noqa: BLE001
        logger.exception("shopper_v2 order_status: could not record case")
        return ("I couldn't look that up right now - please try again.", None)

    if str(risk_decision.outcome) != "AUTO":
        return ("I can't look that up right now - someone needs to approve it first.", None)
    if case_id is None:
        return ("I couldn't look that up right now - please try again.", None)

    executed = await execution.execute_case(connection_id, case_id)
    if not executed.succeeded:
        # Real reasons only: no order number given, or the platform genuinely
        # has no such order (or refused) - the dispatch deliberately gives
        # the same answer for both, so a stranger cannot learn which ids
        # are real by walking the numbering.
        return (executed.summary, None)

    payload = executed.payload or {}
    reply = executed.shopper_summary or executed.summary

    # Line-item disclosure only after a real ownership check - never assumed
    # from the fact that this session happened to ask about this order id.
    lines = payload.get("lines") or []
    real_order_id = payload.get("order_id")
    if owner_keys and real_order_id and lines:
        owner = await db.owners.owner_of(connection_id, db.owners.ORDER, real_order_id)
        if owner in owner_keys:
            items = ", ".join(
                f"{line['quantity']} x {line['title']}" if line.get("quantity", 1) != 1 else line["title"]
                for line in lines
            )
            reply = f"{reply} It had {items}."

    return (reply, payload)
