"""Execute one cart action the shopper has ALREADY, explicitly authorized.

Extracted from `engine/api/chat.py`'s own private `_run_trusted_cart_action`
- the exact same Decision Engine -> Risk Gate -> `db.record_case` ->
`ExecutionService.execute_case` sequence V1 already runs whenever a
shopper's own tap (never a model guess) is what authorizes a specific
action: a tapped product/variant button, or accepting a stored proposal.
Pulled out to this neutral module - under `engine/execution/`, which both
V1 and Shopper V2 already import from freely - so `engine/shopper_v2/`'s
own architecture boundary (V2 may never import `engine.api.chat` - see
`orchestrator.py`'s docstring) does not have to be worked around to reuse
it for V2's own trusted-tap moment (`engine/shopper_v2/fulfillment.py`'s
variant-selection button).

**This does not weaken invariant 1 ("the AI cannot assert risk").** The
candidate built here still goes through the real `DecisionEngine`/
`RiskGate` exactly like a model-proposed action would - a tap authorizes
WHICH product/variant is being acted on, never what its risk classification
is. `FINANCIAL_ALWAYS_HUMAN` and every other merchant policy rule still run
unconditionally; `awaiting=True` here means exactly what it means at
`accept_proposal` time - a human must approve before anything executes.
"""

from __future__ import annotations

import logging

from engine import db
from engine.decision import DecisionEngine, operation_for
from engine.risk import RiskGate
from shared.models import ActionType, ProposedAction

from .service import Executed, ExecutionService

logger = logging.getLogger(__name__)


async def run_trusted_action(
    *,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    cart_id: str,
    action_type: ActionType,
    parameters: dict,
    rationale: str,
) -> tuple[Executed | None, str | None, bool]:
    """Returns `(executed_or_None, case_id_or_None, awaiting)`.
    `awaiting=True` means the Risk Gate did not clear this for automatic
    execution - the caller must tell the shopper it needs merchant
    approval and must NOT treat anything as having happened to the cart.
    Never called with a target the shopper has not explicitly chosen -
    that check is the CALLER's job (see `fulfillment.py::_match_answer`
    for the one this module was built for); this function trusts the
    target it is given exactly as much as `accept_proposal` already
    trusts an accepted proposal's own stored fields."""
    candidate = ProposedAction(
        action_type=action_type,
        operation=operation_for(action_type),
        parameters=parameters,
        rationale=rationale,
        confidence=1.0,
    )
    trace = await decision.decide([candidate], connection_id=connection_id, friction=None)
    risk_decision = gate.classify(trace.selected)
    awaiting = str(risk_decision.outcome) != "AUTO"

    case_id = None
    try:
        case_id = await db.record_case(
            connection_id=connection_id,
            friction=None,
            query=rationale[:300],
            cart_id=cart_id,
            order_id=None,
            session_id=session_id,
            reasoning={
                "used_model": False,
                "model_name": None,
                "diagnosis": None,
                "evidence": [],
                "fallback_reason": "shopper explicitly chose this via a conversational tap/exact match",
                "model_reply": None,
                "shopper_reply": None,
                "prompt_tokens": None,
                "completion_tokens": None,
            },
            decision={
                "proposed": [
                    {
                        "action_type": str(candidate.action_type),
                        "parameters": candidate.parameters,
                        "rationale": candidate.rationale,
                        "confidence": candidate.confidence,
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
        logger.exception("run_trusted_action: could not record case; continuing")

    if awaiting or not case_id:
        return None, case_id, awaiting
    executed = await execution.execute_case(connection_id, case_id)
    return executed, case_id, awaiting
