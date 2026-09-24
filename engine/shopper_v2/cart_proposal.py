"""Phase 4/continuation: cart proposal + shopper approval + execution
integration - reusing the EXISTING Decision Engine, Risk Gate,
ExecutionService, and cart-proposal storage exactly as V1's
`engine/api/chat.py` does, not a second implementation of any of them.

**The one hard safety rule this module enforces, in code, not just by
convention: V2 can never construct anything except a
`ActionType.PROPOSE_CART_CHANGE` candidate.** Whatever a shopper's message
implies ("add that to my cart", "remove the shoes", "make that quantity
two"), this module never builds an
`ADD_TO_CART`/`REMOVE_CART_LINE`/`UPDATE_CART_QUANTITY`/`CLEAR_CART`
candidate directly - those are exactly the action types that actually
mutate a cart, and none of them is reachable from anything in this
package. A cart only ever changes after a SEPARATE, explicit shopper call
to the EXISTING, unmodified `POST /api/chat/proposals/{id}/accept` route
(`engine/api/chat.py::accept_proposal`) - which itself re-validates the
proposal against live data before running the real mutation through the
real pipeline. This mirrors invariant 4 ("a tap is trusted; the model's
guess is not") for a path with no tap UI yet: the model's inferred target
is never enough on its own; a second, explicit shopper action is always
required before anything in a cart actually changes.

`PROPOSE_CART_CHANGE`'s own `proposal_change_type` supports "ADD",
"REMOVE", "CHANGE_QUANTITY", and (as of the V1<->V2 parity migration)
"CLEAR" (`accept_proposal`/`execute_case`'s dispatch, both extended there,
not forked or duplicated here) - this module maps this package's own
`operation` vocabulary ("add"/"remove"/"set_quantity"/"clear") onto that
existing, real set. "clear" (emptying the whole cart) needs no product
reference at all - `intent_resolution.py` already returns
`operation="clear"` with no `product_id` for exactly this case. Genuinely
STRICTER than V1's own direct-execute `ActionType.CLEAR_CART` (no proposal
step there): a V2-originated clear is always propose-then-approve, same as
every other change_type this module builds - never a weaker copy of V1's
behavior, never a new commerce implementation (the actual clearing still
happens through `ActionType.CLEAR_CART`'s existing, unmodified dispatch,
reached only after the shopper's own explicit accept).

Call path, identical to V1's (see `engine/api/chat.py` around its own
`engine.decision.decide` / `engine.gate.classify` / `db.record_case` /
`engine.execution.execute_case` calls) - reused by reference, not
reimplemented:

    ProposedAction(PROPOSE_CART_CHANGE)
        -> decision.decide([candidate], connection_id=...)
        -> gate.classify(trace.selected)
        -> db.record_case(...)                      # same function V1 calls
        -> execution.execute_case(connection_id, case_id)  # same function V1 calls
        -> db.cart_proposals.create(...) happens INSIDE execute_case's own
           dispatch (engine/execution/service.py's PROPOSE_CART_CHANGE
           case) - not called again here.

This module imports `engine.decision`, `engine.risk`, `engine.execution`,
and `engine.db` - but calls only their existing PUBLIC methods, and never
imports or calls anything from `engine.api.chat`, any adapter WRITE method
directly, or any payment-related action type.
"""

from __future__ import annotations

import logging

from engine.db import record_case
from engine.decision import DecisionEngine, operation_for
from engine.risk import RiskGate
from engine.execution import ExecutionService
from shared.models import ActionType, ProposedAction

from .state import ShoppingStateV2

logger = logging.getLogger(__name__)

_NO_CART_REPLY = (
    "I don't have a cart to work with for this conversation yet - please "
    "start one first."
)
_NO_PRODUCT_REPLY = (
    "I don't have a specific product in view to add - ask me to find "
    "something first, or tell me exactly which one."
)
_NO_TARGET_REPLY = (
    "I don't have a specific cart item to change - tell me exactly which "
    "one."
)
_NEEDS_APPROVAL_REPLY = (
    "I can suggest that, but someone at the shop needs to approve it "
    "first - I've passed it on."
)
#: This package's own operation vocabulary -> the real
#: `proposal_change_type` values `accept_proposal`'s dispatch understands
#: (engine/api/chat.py / engine/execution/service.py - CLEAR added there
#: as part of the V1<->V2 parity migration, ADD/REMOVE/CHANGE_QUANTITY
#: pre-existing and unmodified).
_CHANGE_TYPE_FOR_OPERATION = {
    "add": "ADD",
    "remove": "REMOVE",
    "set_quantity": "CHANGE_QUANTITY",
    "clear": "CLEAR",
}


async def run_cart_change_proposal_turn(
    *,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    cart_id: str | None,
    message: str,
    state: ShoppingStateV2,
    operation: str,
    hinted_product_id: str | None,
    quantity: int | None = None,
    mission_item_id: str | None = None,
    reason: str | None = None,
    variant_id: str | None = None,
) -> tuple[str, dict | None]:
    """Returns (reply, proposal_dict_or_None). The proposal dict (when
    present) is exactly what `db.cart_proposals.create` returned -
    `proposal["proposal_id"]` is what the shopper's client passes to the
    EXISTING `/api/chat/proposals/{id}/accept` (or `/reject`) route to
    actually act on it. Nothing in this function mutates a cart.

    `operation` is one of "add"/"remove"/"set_quantity"/"clear". "clear"
    needs no product reference at all - `intent_resolution.py` already
    returns it with no `product_id`, and no product-resolution step below
    applies to it.

    `variant_id`, when given, is a real, already-verified variant id - only
    ever supplied by `fulfillment.py` after the SHOPPER's own explicit
    choice (a button tap, or an exact-text match against a real option
    label) for a product that needed one, mirroring invariant 4 ("a tap is
    trusted; the model's guess is not") for the propose-time path exactly
    as `engine/execution/service.py`'s own `chosen_variant` already does for
    the accept-time path. Never a guess this module makes itself.

    `mission_item_id`, when given (only ever by
    `basket_proposal.py`, for a multi-item mission's own proposals), is
    opaque metadata threaded straight through to the stored
    `CartProposal.mission_item_id` column - never interpreted here, never
    trusted as proof of anything, purely so a caller can later tell which
    proposal fulfills which tracked item. `reason`, when given, overrides
    the default "you asked me to" - `basket_proposal.py` uses this to name
    the mission item in the stored, ops-visible proposal reason."""
    if not cart_id:
        return (_NO_CART_REPLY, None)

    change_type = _CHANGE_TYPE_FOR_OPERATION.get(operation)
    if change_type is None:
        return (_NO_TARGET_REPLY, None)

    if operation == "clear":
        product_id = None
        quantity_param = None
    else:
        # Resolution order: the model's own hint, IF it turns out to name a
        # real product (verified inside execute_case's own
        # PROPOSE_CART_CHANGE dispatch, via a real adapter.get_product call
        # - not re-verified here, to avoid a second, redundant fetch) -
        # otherwise, for "add" only, if exactly one product is currently
        # under consideration (`active_products`, set by a prior discovery/
        # compare turn), that one. Never guessed at when ambiguous, and
        # never a fallback at all for remove/set_quantity (a cart-line
        # reference is expected to already be resolved by
        # `intent_resolution.py`'s cart-aware resolution before this
        # function is ever called for those operations).
        product_id = hinted_product_id
        if not product_id and operation == "add":
            candidates = list(dict.fromkeys(state.active_products))
            if len(candidates) == 1:
                product_id = candidates[0]

        if not product_id:
            return (_NO_PRODUCT_REPLY if operation == "add" else _NO_TARGET_REPLY, None)

        quantity_param = quantity if operation == "set_quantity" else None

    proposed = ProposedAction(
        action_type=ActionType.PROPOSE_CART_CHANGE,
        operation=operation_for(ActionType.PROPOSE_CART_CHANGE),
        parameters={
            "product_id": product_id,
            "proposal_change_type": change_type,
            "quantity": quantity_param,
            "variant_id": variant_id,
            "proposal_reason": reason or "you asked me to",
            "mission_item_id": mission_item_id,
            "said": message[:80],
        },
        rationale=f"shopper asked to {operation} a specific item",
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
                "used_model": False,
                "model_name": None,
                "diagnosis": None,
                "evidence": [],
                "fallback_reason": None,
                "model_reply": None,
                "shopper_reply": None,
                "prompt_tokens": None,
                "completion_tokens": None,
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
                    {
                        "action_type": str(r.action_type),
                        "reason": r.reason,
                        "detail": r.detail,
                    }
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
        logger.exception("shopper_v2 cart_proposal: could not record case")

    if str(risk_decision.outcome) != "AUTO":
        return (_NEEDS_APPROVAL_REPLY, None)

    if case_id is None:
        return (_NO_PRODUCT_REPLY, None)

    executed = await execution.execute_case(connection_id, case_id)
    if not executed.succeeded:
        return (
            f"I couldn't put that proposal together - {executed.summary}",
            None,
        )

    proposal = executed.payload.get("proposal")
    title = executed.payload.get("product_title") or "that item"
    proposal_id = proposal["proposal_id"] if proposal else None

    verb = {
        "add": f"add {title} to your cart",
        "remove": f"remove {title} from your cart",
        "set_quantity": f"change {title} to quantity {quantity or 1}",
        "clear": "clear your whole cart",
    }[operation]
    qty_note = (
        f" You asked for {quantity} - I can only propose one for now; "
        "add more the same way once this one's approved."
        if operation == "add" and quantity and quantity > 1
        else ""
    )
    reply = (
        f"I'd like to {verb}.{qty_note} Confirm to go ahead - proposal "
        f"{proposal_id} is ready for your approval."
        if proposal_id
        else f"I'd like to {verb}, but couldn't record the proposal - "
        "please try again."
    )
    return (reply, proposal)
