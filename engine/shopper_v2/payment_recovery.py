"""Phase 5 continuation: Payment recovery, capability-aware.

Reuses the EXISTING Decision Engine / Risk Gate / ExecutionService exactly
as `checkout.py`/`cart_proposal.py` already do - no new commerce logic.

The shopper describes a payment problem in their own words; this module
proposes all three declared recovery ActionTypes
(`RETRY_PAYMENT`/`OFFER_ALTERNATE_PAYMENT`/`SPLIT_PAYMENT` - all map to
the same `Operation.RECOVER_PAYMENT`, `engine/decision/engine.py`,
unmodified) as CANDIDATES and lets the real, unmodified `DecisionEngine`
decide what survives: on a platform that never declared
`Operation.RECOVER_PAYMENT` (Northfield), none of the three survive its
capability filter and it automatically returns `ESCALATE_TO_HUMAN`
instead (`engine/decision/engine.py`'s own "when nothing survives"
behaviour, unmodified) - the exact same capability-aware branch V1's
recovery flow already demonstrates. On a platform that does
(Kettle), one of the three survives the capability filter, but
**`ACTION_RISK_PROPERTIES` declares all three `financial=True`**
(`shared/models/action.py`, unmodified) - the Risk Gate's
`FINANCIAL_ALWAYS_HUMAN` rule fires before any merchant policy and cannot
be switched off, so it is unconditionally classified `HUMAN`, never
`AUTO`. **This module therefore never calls `execution.execute_case` for
an actual recovery action - only `decision.decide`/`gate.classify`/
`db.record_case`, which is exactly enough to place a real, visible case in
the existing ops queue** (`db.record_case`'s own docstring: "writes a
case, and an approval when one is needed"). Only `ESCALATE_TO_HUMAN`
itself (non-financial, auto-approved) is ever executed - its own dispatch
just records the handover, the same thing V1's own escalation path does.

Money never moves from this module, ever, on any platform - it can only
ever produce a candidate for a human to review, or a handover.
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
    "I don't have a cart to look into for this conversation yet - please "
    "start one first."
)

_RECOVERY_CANDIDATES = (
    ActionType.RETRY_PAYMENT,
    ActionType.OFFER_ALTERNATE_PAYMENT,
    ActionType.SPLIT_PAYMENT,
)


async def run_payment_recovery_turn(
    *,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    cart_id: str | None,
    message: str,
) -> tuple[str, dict | None]:
    """Returns (reply, trace_summary_or_None). Never executes a financial
    action - see this module's own docstring for why that is structurally
    guaranteed, not just a runtime choice made here."""
    if not cart_id:
        return (_NO_CART_REPLY, None)

    candidates = [
        ProposedAction(
            action_type=action_type,
            operation=operation_for(action_type),
            parameters={"said": message[:80]},
            rationale="shopper reported a payment problem",
            confidence=0.6,
        )
        for action_type in _RECOVERY_CANDIDATES
    ]

    trace = await decision.decide(candidates, connection_id=connection_id)
    risk_decision = gate.classify(trace.selected)
    selected_type = trace.selected.action.action_type

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
                        "action_type": str(c.action_type),
                        "parameters": c.parameters,
                        "rationale": c.rationale,
                        "confidence": c.confidence,
                    }
                    for c in candidates
                ],
                "rejected": [
                    {"action_type": str(r.action_type), "reason": r.reason, "detail": r.detail}
                    for r in trace.rejected
                ],
                "selected_action": str(selected_type),
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
        logger.exception("shopper_v2 payment_recovery: could not record case")
        return (
            "I couldn't log that for a person to look at right now - please try again.",
            None,
        )

    summary = {
        "case_id": case_id,
        "selected_action": str(selected_type),
        "risk_outcome": str(risk_decision.outcome),
        "escalated_because_unsupported": trace.escalated_because_empty,
    }

    if trace.escalated_because_empty:
        # This platform never declared payment-recovery capability at all
        # (Northfield's own shape) - ESCALATE_TO_HUMAN is non-financial
        # and auto-approved, so it runs now, the same way V1's own
        # escalation path does.
        executed = await execution.execute_case(connection_id, case_id)
        return (
            "This shop can't automatically retry or offer an alternative "
            "for a declined payment, so I've passed it to a person there - "
            "they'll follow up with you.",
            summary,
        )

    if str(risk_decision.outcome) != "AUTO":
        # A real recovery method IS available on this platform, but every
        # recovery action type is financial, so it always needs a human -
        # never executed here. The case (and its approval row) already
        # exists in the real ops queue at this point.
        return (
            "This shop can help recover that payment, but a person needs "
            "to approve it first - I've flagged it and they'll follow up "
            "with you shortly.",
            summary,
        )

    # Should not normally happen (every recovery candidate is financial),
    # but if a merchant's own policy or a future action type ever changes
    # that, degrade to the same honest "flagged" message rather than
    # silently executing something unexpected.
    return (
        "I've logged that for review - someone will follow up with you shortly.",
        summary,
    )
