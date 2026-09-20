"""Cart intelligence / "what's in my cart" - via the EXISTING
`CHECK_CART_STATUS` dispatch (`engine/execution/service.py`, unmodified) -
the same decision -> risk -> record_case -> execute_case shape
`order_status.py` and `checkout.py` already use for a read-only,
non-financial, auto-approved single-turn answer.

Closes a real V1 -> V2 parity gap: V1's `engine/api/chat.py` has a
dedicated `CHECK_CART_STATUS` branch (chat.py:1680) precisely because
"show cart"/"what's in my cart" used to fall through to an unrelated
browse/recommend answer - the exact live-reproduced bug that branch's own
comment documents. V2's `capability.py` already routes both
`CART_INTELLIGENCE` and `CHECK_CART_STATUS` model hints to
`Capability.CART_STATUS`, but until this module, `orchestrator.py` treated
that capability as "not yet available through V2" (an honest decline, not
a wrong answer - but still a real, closeable gap, not a safety issue).

Purely a read (`adapter.get_cart` inside the existing dispatch) - no
proposal, no mutation, nothing for `resolve_cart_target` to arbitrate.
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
    "I don't have a cart to check for this conversation yet - start "
    "shopping and I'll be able to tell you what's in it."
)


async def run_cart_status_turn(
    *,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    cart_id: str | None,
    message: str,
) -> tuple[str, dict | None]:
    """Returns (reply, payload_or_None). Never invents cart contents - the
    reply is always `executed.shopper_summary`/`summary` from the real,
    unmodified `CHECK_CART_STATUS` dispatch, which itself reads
    `adapter.get_cart` fresh every call."""
    if not cart_id:
        return (_NO_CART_REPLY, None)

    proposed = ProposedAction(
        action_type=ActionType.CHECK_CART_STATUS,
        operation=operation_for(ActionType.CHECK_CART_STATUS),
        parameters={"said": message[:80]},
        rationale="shopper asked what is in their cart",
        confidence=0.9,
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
    except Exception:  # noqa: BLE001 - never let case-recording failure crash the turn
        logger.exception("shopper_v2 cart_status: could not record case")
        return ("I couldn't check your cart right now - please try again.", None)

    if str(risk_decision.outcome) != "AUTO":
        return ("I can't check your cart right now - someone needs to approve it first.", None)
    if case_id is None:
        return ("I couldn't check your cart right now - please try again.", None)

    executed = await execution.execute_case(connection_id, case_id)
    if not executed.succeeded:
        return (executed.summary, None)

    reply = executed.shopper_summary or executed.summary
    return (reply, executed.payload)
