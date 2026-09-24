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
    #: Batch 4: real analysis over the actual cart + fresh product data +
    #: current shopping_state (engine/execution/cart_intelligence.py,
    #: unmodified) - duplicates, over-budget total, a missing declared
    #: complement, an unavailable line, a stated exclusion sitting in the
    #: cart. Distinct from CART_STATUS (a bare contents echo) - see
    #: cart_intelligence_v2.py's own docstring.
    CART_INTELLIGENCE = "cart_intelligence"
    CART_ACTION = "cart_action"
    CHECKOUT = "checkout"
    PAYMENT_RECOVERY = "payment_recovery"
    ORDER_STATUS = "order_status"
    #: A dead search or a rejected promo code - see `friction_recovery.py`.
    #: Reached only via an explicit `ChatRequestV2.friction` signal from
    #: the frontend, never inferred from the model's own hint (there is no
    #: `_ACTION_TYPE_TO_CAPABILITY` entry for this - a shopper's own
    #: message never routes here by accident).
    FRICTION_RECOVERY = "friction_recovery"
    #: A greeting, thanks, acknowledgement, small talk, or "what can you
    #: do?" - see `conversational.py`. Reached only when the model itself
    #: judged, on this same turn's call, that nothing here needs a catalog
    #: lookup or an action (`message_intent == "conversational"`) AND no
    #: specific `hinted_action_type` was ALSO proposed - a real shopping
    #: signal always wins even if the model additionally chatted. Never a
    #: phrase-list match; see `route`'s own docstring for the exact rule.
    CONVERSATIONAL = "conversational"
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
    # Batch 4 fix: these used to collapse into the SAME capability
    # (CART_STATUS), so a shopper asking "does my cart look okay" got the
    # identical bare contents-echo as "what's in my cart" - the model's own
    # CART_INTELLIGENCE hint (a real, distinct ActionType with its own
    # dispatch in engine/execution/service.py, analyzing duplicates/budget/
    # missing complements/exclusion conflicts) was silently discarded.
    # Un-collapsed so the analysis is actually reachable - see
    # cart_intelligence_v2.py.
    "CART_INTELLIGENCE": Capability.CART_INTELLIGENCE,
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


#: Closed-class comparison vocabulary - the SAME discipline
#: `engine/execution/relevance.py`'s `_STOPWORDS`/`_GENERIC_ACTION_VERBS`
#: already use elsewhere in this codebase (a small, fixed, structurally
#: reliable set of words that mean the same thing regardless of what else
#: is in the sentence), not a per-example phrase patch. Live-reproduced
#: gap this exists to close: the PRIMARY model call's own
#: `hinted_action_type` for "compare both" (after "show me shoes and
#: socks" tracked two real mission items) classified as
#: `COMPARE_PRODUCTS` only 1 of 3 identical repeated calls - genuine
#: sampling variance in the primary classifier, the same class of problem
#: `intent_resolution.py`'s own SEPARATE, deterministic cart-reference
#: interpreter already exists to fix for cart intent. This is the
#: comparison-intent equivalent: a real word from this closed set, present
#: in the shopper's own raw message, is treated as comparison intent
#: whatever the primary classifier's own hint said - checked against REAL
#: tracked products before ever routing there (see `node_route_capability`
#: in graph.py), never a guess on its own.
_COMPARISON_WORDS = (
    "compare", "comparison", "versus", " vs ", "vs.", "difference between",
    "which is better", "which one is better", "which one's better",
)


#: Closed-class temporal-reference vocabulary - same discipline as
#: `_COMPARISON_WORDS` above. A shopper asking about something "earlier"/
#: "before"/"you showed me" is asking about a REAL PAST group from the
#: rolling ledger (`ShoppingStateV2.recent_product_groups`), not the
#: current turn's own narrow topic - live-reproduced gap: "what was the
#: cheapest shoe you showed me earlier" after a topic switch to socks
#: answered from whatever single id `recent_relevant_product_ids`'s
#: minimum-based walk happened to grab first, not the real, full,
#: matching historical group - see compare.py's own
#: `_best_matching_historical_group`.
_HISTORICAL_REFERENCE_WORDS = (
    "earlier", "before", "previously", "you showed", "you had shown",
    "showed me", "showed earlier", "that first one", "originally",
)


def looks_like_historical_reference(message: str) -> bool:
    """True when `message` itself contains a real, closed-class temporal-
    reference word - never sufficient on its own to answer anything; the
    caller still requires a real, matching historical group before
    trusting this."""
    lowered = f" {message.lower().strip()} "
    return any(word in lowered for word in _HISTORICAL_REFERENCE_WORDS)


#: Closed-class recommendation-follow-up vocabulary - same discipline as
#: the comparison/temporal-reference lists above. Live-reproduced gap:
#: "compare both" then "what do you recommend?" routed to plain discovery
#: (a literal, blank-query search for the words "what do you recommend"),
#: instead of being recognized as a follow-up asking for a grounded pick
#: among the products just discussed.
_RECOMMENDATION_WORDS = (
    "what do you recommend", "which one do you recommend", "which one should i",
    "what would you recommend", "what would you suggest", "which one is better for me",
    "what's your recommendation", "which one do you suggest",
)


def looks_like_recommendation_request(message: str) -> bool:
    """True when `message` itself contains a real, closed-class
    recommendation-follow-up phrase - never sufficient alone; the caller
    still requires 2+ real, already-tracked products before trusting
    this."""
    lowered = f" {message.lower().strip()} "
    return any(word in lowered for word in _RECOMMENDATION_WORDS)


def looks_like_comparison_request(message: str) -> bool:
    """True when `message` itself contains a real, closed-class comparison
    word - never a guess at the shopper's broader intent, and never
    sufficient on its own to route to Capability.COMPARE (the caller still
    requires 2+ real, already-tracked products before trusting this)."""
    lowered = f" {message.lower().strip()} "
    return any(word in lowered for word in _COMPARISON_WORDS)


def route(
    hinted_action_type: str | None, message_intent: str | None = None,
) -> Capability:
    """Pure classification, no side effect.

    A specific `hinted_action_type` always wins, whatever `message_intent`
    says - a shopper who chats AND names a real action gets the action.
    Only when the model proposed no specific action does `message_intent`
    get a say: `None` (unclassified - the model was unavailable, or this
    call predates the classification field) keeps the exact previous
    default, DISCOVER, so nothing regresses when classification itself
    isn't available. Only a POSITIVE `"conversational"` judgment - a real,
    per-turn call the model made on this same turn - routes to
    `Capability.CONVERSATIONAL` instead. This is the structural fix for a
    live-reproduced bug (see `understanding.py`'s own docstring): every
    greeting/thanks/acknowledgement used to default straight to DISCOVER
    and run a real catalog search against it."""
    if hinted_action_type is not None:
        return _ACTION_TYPE_TO_CAPABILITY.get(hinted_action_type, Capability.UNKNOWN)
    if message_intent == "conversational":
        return Capability.CONVERSATIONAL
    return Capability.DISCOVER


#: Closed-class cart-intelligence vocabulary - same discipline as the
#: comparison/temporal-reference/recommendation lists above. Live-
#: reproduced via Playwright: "My cart is over 10000. What can I change to
#: bring it under budget?" routed to plain DISCOVER (a literal catalog
#: search for those words, "none of them are a confident match") instead
#: of `Capability.CART_INTELLIGENCE` - `CART_INTELLIGENCE` has no
#: deterministic backstop at all today, relying entirely on the primary
#: model's own `hinted_action_type` classification, which this exact
#: phrasing did not reliably produce.
#: Regression fix (2026-09-24, live-reproduced via `test_phase13_cart_status.py`):
#: "my cart"/"in my cart"/"the cart" removed - they matched a BARE "what's
#: in my cart?" status question just as readily as a genuine budget-
#: analysis one, and once `SOFT_CAPABILITIES` (below) made `CART_STATUS`
#: itself overridable by this same backstop, that bare question got
#: reclassified from a plain contents echo into a full cart-intelligence
#: analysis it never asked for - confusing the two capabilities the same
#: way `cart_intelligence_v2.py`'s own docstring says they must never be
#: conflated. Every phrase kept below is genuinely budget/cost-analysis-
#: shaped, never satisfied by a bare "what's in my cart" - the original
#: live-reproduced case this list exists for ("My cart is over 10000. What
#: can I change to bring it under budget?") still matches on "bring it
#: under" alone.
_CART_INTELLIGENCE_WORDS = (
    "over budget", "under budget", "bring it under", "within budget",
    "too expensive", "cart total", "bring it down", "over my budget",
)


def looks_like_cart_intelligence_request(message: str) -> bool:
    """True when `message` itself contains a real, closed-class cart-
    intelligence phrase - never sufficient alone; the caller still
    requires a real, non-empty cart before trusting this (an empty cart
    has nothing for `cart_intelligence_v2.py` to analyze)."""
    lowered = f" {message.lower().strip()} "
    return any(word in lowered for word in _CART_INTELLIGENCE_WORDS)


#: Manual-test finding: the deterministic backstops above only ever
#: overrode `capability` when `route()`'s own single-shot classification
#: had landed on DISCOVER or CONVERSATIONAL - the two "the model was not
#: decisive about anything else" defaults. A REAL routing-precedence bug,
#: not tied to any one phrase: whenever that same single primary call
#: instead guessed one of the OTHER capabilities below - COMPATIBILITY,
#: COMPLEMENTARY, TRADE_OFF, CART_STATUS, CART_INTELLIGENCE, ORDER_STATUS -
#: every backstop was structurally unreachable, because none of those
#: guesses ever gets checked against anything beyond the model's own single
#: call either. Live-reproduced: "what was the cheapest shoe you showed me
#: earlier" landing on COMPATIBILITY instead of DISCOVER/CONVERSATIONAL
#: skipped the historical-reference backstop entirely, for no reason tied
#: to that phrase - ANY of these six guesses being wrong would have hidden
#: the exact same bug for a differently-worded reference. `SOFT_CAPABILITIES`
#: is every capability value reachable from nothing but that one raw guess,
#: with no independent, deterministic confirmation anywhere downstream -
#: `COMPARE` (a 2+-tracked-product check), `CART_ACTION`
#: (`resolve_cart_target`), `CHECKOUT`/`PAYMENT_RECOVERY`/
#: `FRICTION_RECOVERY` (an explicit signal or financial gate) all have
#: their own separate confirmation and are deliberately excluded - a
#: reference backstop that finds real, structured evidence must be free to
#: override every equally-unconfirmed guess, not only the two the model
#: happens to default to.
SOFT_CAPABILITIES = frozenset(
    {
        Capability.DISCOVER,
        Capability.CONVERSATIONAL,
        Capability.COMPATIBILITY,
        Capability.COMPLEMENTARY,
        Capability.TRADE_OFF,
        Capability.CART_STATUS,
        Capability.CART_INTELLIGENCE,
        Capability.ORDER_STATUS,
        Capability.UNKNOWN,
    }
)


def looks_like_reference(message: str) -> bool:
    """Union of every closed-class marker meaning 'point at something
    already shown or discussed' rather than naming a new search: a
    comparison word, a recommendation follow-up, a temporal/historical
    marker, or an ordinal/collective reference ("the first one", "both",
    "all three"). Never sufficient alone - every caller still requires
    real tracked state before trusting this, exactly like each of the
    individual checks it combines."""
    from .reference_resolution import (
        looks_like_comparative_pair_reference,
        looks_like_group_or_ordinal_reference,
    )

    return (
        looks_like_comparison_request(message)
        or looks_like_recommendation_request(message)
        or looks_like_historical_reference(message)
        or looks_like_group_or_ordinal_reference(message)
        # "which one was cheaper"/"the pricier one" - a real, closed-class
        # comparative-pair reference, structurally identical to the
        # ordinal/collective markers just above but not itself a
        # "compare"/"versus"-shaped `_COMPARISON_WORDS` hit - live-
        # reproduced gap: this bare phrasing never overrode the primary
        # model's own SOFT guess, so it never reached COMPARE at all.
        or looks_like_comparative_pair_reference(message)
    )
