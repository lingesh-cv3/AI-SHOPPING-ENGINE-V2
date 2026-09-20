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

**DISCOVERY vs CART_ACTION - a hard rule, not a phrase list.** A live,
real-shopper-reproduced bug: "I need shoes" (plain discovery) produced a
cart proposal, because the PRIMARY model call's own `hinted_action_type` -
the model's raw, unvalidated first-pass guess at what the shopper wants,
made before any real product is even shown - was trusted by `route()`
below to mean the same thing as the shopper actually authorizing a cart
mutation. A model asked to name an action for a shopping-goal statement
will sometimes guess `ADD_TO_CART` even though the shopper never said
anything resembling "add"/"buy"/"take" - the model is not being asked
"is this cart intent", it is being asked "what single action fits this
turn", and for a strong product statement it can pick the closest-sounding
action label even when that's wrong.

So `_ACTION_TYPE_TO_CAPABILITY` below deliberately does NOT map any
cart-mutation `ActionType` (`ADD_TO_CART`/`REMOVE_CART_LINE`/
`UPDATE_CART_QUANTITY`/`CLEAR_CART`/`PROPOSE_CART_CHANGE`) to
`Capability.CART_ACTION`. `route()`'s mapping is a routing HINT only, and
it is structurally forbidden from ever authorizing a cart mutation by
itself - `Capability.CART_ACTION` can ONLY be reached one way in this
codebase: `orchestrator.py`'s call to `intent_resolution.py::resolve_cart_target`,
a SEPARATE, narrow, deterministic check that requires the interpreter to
return an explicit, positive `is_cart_intent=True` AND a resolvable real
target before anything downstream ever treats a turn as a cart operation
(see that module's own docstring - `is_cart_intent=False` structurally
means "fall through to whatever `route()` said," which is DISCOVER for any
message with no clear cart-type hint). A raw cart-type hint from the
primary call, with nothing behind it, therefore lands on `Capability.DISCOVER`
here - correct, since a shopping-goal statement IS a discovery request,
and the real gate for a cart action is `resolve_cart_target`, not this
mapping.
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
    # Deliberately NOT mapped to Capability.CART_ACTION - see this module's
    # own docstring. The primary model call's raw guess at one of these
    # ActionTypes is not real cart-intent signal on its own (a live bug:
    # "I need shoes" produced ADD_TO_CART from the primary call). Routing
    # these to DISCOVER means a genuine cart-intent message still gets a
    # correct answer, because `orchestrator.py` runs the SEPARATE,
    # deterministic `resolve_cart_target` check on every turn regardless of
    # what `route()` returns here, and only THAT check may ever set
    # `Capability.CART_ACTION`.
    "ADD_TO_CART": Capability.DISCOVER,
    "REMOVE_CART_LINE": Capability.DISCOVER,
    "UPDATE_CART_QUANTITY": Capability.DISCOVER,
    "CLEAR_CART": Capability.DISCOVER,
    "PROPOSE_CART_CHANGE": Capability.DISCOVER,
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
