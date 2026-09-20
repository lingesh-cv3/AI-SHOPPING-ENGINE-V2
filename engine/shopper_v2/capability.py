"""Phase 1 capability routing skeleton.

Classifies what KIND of help a turn needs, as a read-only routing decision -
never an execution decision. The classification comes from the SAME
`ReasoningService.reason()` tool call `understanding.py` already makes
(reusing its `ProposedAction.action_type`, when present, purely as a
routing label) - no new model call, no new prompt.

`Capability.DISCOVER` (Phase 1-2) and `Capability.COMPARE` (Phase 3) are
real, implemented capabilities. `Capability.CART_ACTION` (Phase 4, first
slice) is also real - it always resolves to a `PROPOSE_CART_CHANGE`
candidate through the existing Decision Engine/Risk Gate/ExecutionService,
never a direct mutation (see `cart_proposal.py` and `orchestrator.py`'s own
docstring for the full safety argument). Every other value is recognized
and routed to an honest "not yet available through V2" response rather
than silently falling through to discovery and answering the wrong
question. COMPATIBILITY/COMPLEMENTARY/TRADE_OFF remain the next Phase 3
work; CHECKOUT/ORDER_STATUS remain future Phase 4/5 work.
"""

from __future__ import annotations

from enum import Enum


class Capability(str, Enum):
    DISCOVER = "discover"
    COMPARE = "compare"
    COMPATIBILITY = "compatibility"
    COMPLEMENTARY = "complementary"
    TRADE_OFF = "trade_off"
    CART_STATUS = "cart_status"
    CART_ACTION = "cart_action"
    CHECKOUT = "checkout"
    PAYMENT_RECOVERY = "payment_recovery"
    ORDER_STATUS = "order_status"
    UNKNOWN = "unknown"


#: Maps a model-proposed ActionType's string value (read ONLY as a routing
#: label - see `understanding.py`'s docstring on why the rest of that
#: action is discarded) onto a V2 capability. Deliberately small and
#: explicit, not a generic mapping of the whole ActionType enum - only
#: entries a real V2 capability exists for, or a name it makes sense to
#: recognize now so the "not yet available" message is specific rather
#: than generic.
_ACTION_TYPE_TO_CAPABILITY: dict[str, Capability] = {
    "RECOMMEND_PRODUCTS": Capability.DISCOVER,
    "ANSWER_PRODUCT_QUESTION": Capability.DISCOVER,
    "CHECK_AVAILABILITY": Capability.DISCOVER,
    "COMPARE_PRODUCTS": Capability.COMPARE,
    "CHECK_COMPATIBILITY": Capability.COMPATIBILITY,
    "SUGGEST_COMPLEMENTARY": Capability.COMPLEMENTARY,
    "CART_INTELLIGENCE": Capability.CART_STATUS,
    "CHECK_CART_STATUS": Capability.CART_STATUS,
    "ADD_TO_CART": Capability.CART_ACTION,
    "REMOVE_CART_LINE": Capability.CART_ACTION,
    "UPDATE_CART_QUANTITY": Capability.CART_ACTION,
    "CLEAR_CART": Capability.CART_ACTION,
    "PROPOSE_CART_CHANGE": Capability.CART_ACTION,
    "PREPARE_CHECKOUT": Capability.CHECKOUT,
    "RETRY_PAYMENT": Capability.PAYMENT_RECOVERY,
    "OFFER_ALTERNATE_PAYMENT": Capability.PAYMENT_RECOVERY,
    "SPLIT_PAYMENT": Capability.PAYMENT_RECOVERY,
    "CHECK_ORDER_STATUS": Capability.ORDER_STATUS,
}


def route(hinted_action_type: str | None) -> Capability:
    """Pure classification, no side effect. `None` (the model proposed
    nothing, or Understanding found nothing) routes to DISCOVER - the same
    default Stage 1 already had before capability routing existed, so a
    turn that classifies ambiguously degrades to the one capability this
    phase actually implements rather than to UNKNOWN."""
    if hinted_action_type is None:
        return Capability.DISCOVER
    return _ACTION_TYPE_TO_CAPABILITY.get(hinted_action_type, Capability.UNKNOWN)
