"""Phase 5, first slice: Checkout continuity.

Reuses the EXISTING `ActionType.PREPARE_CHECKOUT` dispatch
(`engine/execution/service.py`, unmodified) through the real
Decision Engine -> Risk Gate -> ExecutionService pipeline - the same
functions `cart_proposal.py` already calls (`decision.decide`,
`gate.classify`, `db.record_case`, `execution.execute_case`). Unlike
`PROPOSE_CART_CHANGE`, `PREPARE_CHECKOUT` charges nothing and mutates
nothing (`RiskProperties(financial=False, reversible=True)` -
`shared/models/action.py`, unmodified), so it runs to completion in the
same turn - no shopper approval step is needed for showing a total, the
same way V1 already runs it inline.

**Invariant 3 is preserved exactly as V1 already preserves it, not
reimplemented**: this module can show a cart's total and the demo card
list, but it has no path to `/api/chat/pay` at all - that route is a
shopper-tap-only endpoint this module never calls and could not call (it
takes no card/payment data). Actually charging a card remains something
only the shopper's own separate, explicit action can do, through the
existing, unmodified `/api/chat/pay` route.

`execute_case`'s own dispatch already contains the "did the shopper
actually ask to pay" gate (a short, literal word list checked against
`params["said"]`) - reused here unmodified, not duplicated.
"""

from __future__ import annotations

import logging

from engine.db import record_case
from engine.decision import DecisionEngine, operation_for
from engine.risk import RiskGate
from engine.execution import ExecutionService
from shared.models import ActionType, ProposedAction

logger = logging.getLogger(__name__)

_NO_CART_REPLY = (
    "I don't have a cart to check out for this conversation yet - please "
    "start one first."
)


async def run_checkout_turn(
    *,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    cart_id: str | None,
    message: str,
) -> tuple[str, dict | None]:
    """Returns (reply, payload_or_None). `payload`, when present, is
    exactly `Executed.payload` from the real `PREPARE_CHECKOUT` dispatch -
    real cart id, item count, grand total, lines, and the demo card list -
    never anything this module invents. Never claims payment happened;
    never can, since it has no path to `/api/chat/pay`."""
    if not cart_id:
        return (_NO_CART_REPLY, None)

    proposed = ProposedAction(
        action_type=ActionType.PREPARE_CHECKOUT,
        operation=operation_for(ActionType.PREPARE_CHECKOUT),
        parameters={"said": message[:80]},
        rationale="shopper asked about checking out / paying",
        confidence=1.0,
    )

    trace = await decision.decide([proposed], connection_id=connection_id)
    risk_decision = gate.classify(trace.selected)

    case_id = None
    try:
        case_id = await record_case(
            connection_id=connection_id,
            friction=None,
            query=message[:300],
            cart_id=cart_id,
            order_id=None,
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
        logger.exception("shopper_v2 checkout: could not record case")

    if str(risk_decision.outcome) != "AUTO":
        return (
            "I can't show your checkout total right now - someone at the "
            "shop needs to approve it first.",
            None,
        )
    if case_id is None:
        return ("I couldn't prepare checkout right now - please try again.", None)

    executed = await execution.execute_case(connection_id, case_id)
    if not executed.succeeded:
        # Includes: no cart, empty cart, or the shopper didn't actually
        # mention paying - all real, deterministic reasons from the
        # unmodified dispatch, never a raw error.
        return (executed.summary, None)

    payload = executed.payload or {}
    reply = _deterministic_checkout_reply(payload)
    return (reply, payload)


def _deterministic_checkout_reply(payload: dict) -> str:
    lines = payload.get("lines") or []
    item_text = ", ".join(
        f"{line['quantity']} x {line['title']}" if line.get("quantity", 1) != 1 else line["title"]
        for line in lines
    )
    cards = payload.get("cards") or []
    card_text = "; ".join(c.get("label", "") for c in cards)
    return (
        f"Your cart has {payload.get('item_count', 0)} item(s) for "
        f"{payload.get('grand_total', '?')}: {item_text}. To actually pay, "
        f"use your own payment step with a real card - I can't do that "
        f"part for you. Available in this demo: {card_text}."
        if lines
        else "Your cart is ready to check out, but I couldn't read its lines."
    )
