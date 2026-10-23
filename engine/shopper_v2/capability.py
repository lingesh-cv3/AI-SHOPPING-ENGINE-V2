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
    #: Rev. 2 architecture (2026-09) - `engine/shopper_v2/answer.py`. A
    #: question whose SHAPE the shopper explicitly asked for (a
    #: relationship, an explanation, a single recommendation, more
    #: alternatives, a stated-preference re-rank) - decided by
    #: `InterpretationResult.question_type`, a real per-turn model
    #: judgment grounded in the actual conversation, never a fixed phrase
    #: list. Reached only via `node_route_capability`'s own interpretation
    #: call - never `route()`'s bare `hinted_action_type` mapping, which
    #: has no way to know a question's shape.
    ANSWER_QUESTION = "answer_question"
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
    "showed above", "shown above", "listed above", "recommended above", "mentioned above",
)

#: A comparison that points at a whole group already on screen ("compare both",
#: "compare these two", "compare both the shoes showed above") and names no
#: second side. Closed-class markers, same discipline as the lists above:
#: never sufficient alone - the caller still requires two or more real
#: products actually in view.
_COLLECTIVE_MARKERS = (
    " both ", " these ", " those ", " them ", " the two ", " all of ",
    " showed above", " shown above", " listed above",
)
_SECOND_SIDE_MARKERS = (" with ", " against ", " vs", " versus ", " to the ", " and the ", " and a ", " and an ")


def looks_like_collective_comparison(message: str) -> bool:
    """A comparison that selects a GROUP out of what is already in view and
    names no second side. Besides the collective markers above, any message
    the structural group-selector resolver reads as two or more positions
    ("the first two", "the last three", "1 and 3", "the first and third") is
    one - the same resolver `compare` itself uses, not a second word list.
    Step 22 live finding: "compare the first two" after a topic switch was
    not recognised here, so the model's unconfirmed two-sided claim (second
    side = the paused socks topic) decided the turn."""
    lowered = f" {message.lower().strip()} "
    if not looks_like_comparison_request(message):
        return False
    if any(m in lowered for m in _SECOND_SIDE_MARKERS):
        return False
    if any(m in lowered for m in _COLLECTIVE_MARKERS):
        return True
    from .reference_resolution import resolve_group_selector

    # Probed against a large nominal group: only the SHAPE of the selection
    # matters here (is it a multi-item pick?); the real positions are
    # re-resolved against the real view by `compare`.
    selected = resolve_group_selector(message, 50)
    return selected is not None and 2 <= len(selected) < 50


_RESUME_PHRASES = (
    " go back ", " going back ", " back to ", " return to ", " returning to ", " switch back ",
    " come back to ", " take me back ", " back on ",
)


def looks_like_resume_request(message: str) -> bool:
    """True when `message` asks to GO BACK to something discussed earlier
    ("go back to the running shoes I saw earlier") rather than to compare or
    point at products. Such a message carries a historical word ("earlier")
    that would otherwise make the reference backstop route it to COMPARE and
    answer "I don't have two products in view"; it is a topic resume, handled
    by the Focus Stack. Closed-class phrases, never sufficient alone to decide
    meaning - a comparison word always wins."""
    if looks_like_comparison_request(message):
        return False
    lowered = f" {message.lower().strip()} "
    return any(phrase in lowered for phrase in _RESUME_PHRASES)


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


#: A message whose ONLY content is a recommendation verb plus these empty
#: words is a bare request to pick from what is already in view ("recommend
#: one", "recommend any one please", "pick one"). Structural, not a phrase
#: list: a single extra content word ("recommend a waterproof trail shoe",
#: "recommend socks") makes it a search again. Live-reproduced (Step 22,
#: OpenAI gpt-4o-mini, 3 of 3): the model read "recommend one" after a
#: comparison as a discovery search for the word "recommend", found nothing,
#: and the reference backstop never fired because this detector did not
#: recognise the message.
_RECOMMEND_VERBS = frozenset({"recommend", "suggest", "pick", "choose", "select"})
_RECOMMEND_EMPTY_WORDS = frozenset({
    "one", "any", "please", "for", "me", "just", "of", "them", "these", "those", "the", "two", "both",
    "something", "anything",
})


def looks_like_bare_recommendation_imperative(message: str) -> bool:
    import re

    words = re.findall(r"[a-z']+", message.lower())
    return (
        bool(words)
        and any(w in _RECOMMEND_VERBS for w in words)
        and all(w in _RECOMMEND_VERBS or w in _RECOMMEND_EMPTY_WORDS for w in words)
    )


def looks_like_recommendation_request(message: str) -> bool:
    """True when `message` itself contains a real, closed-class
    recommendation-follow-up phrase, or is a bare recommendation imperative
    (a recommendation verb and nothing else of substance) - never
    sufficient alone; the caller still requires 2+ real, already-tracked
    products before trusting this."""
    lowered = f" {message.lower().strip()} "
    return any(word in lowered for word in _RECOMMENDATION_WORDS) or looks_like_bare_recommendation_imperative(message)


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


#: Forensic gap fix (2026-09): "What is my preference now?" - a genuine
#: question about the shopper's OWN currently-stated state (priorities/
#: must-haves/budget), not about any product - had no shape for
#: `question_type` to name at all, so it fell straight through to a plain
#: literal catalog search for the sentence itself ("no_candidates").
#: Same closed-class-word-list discipline as `_CART_INTELLIGENCE_WORDS`
#: immediately above (a deterministic backstop, never a model call, never
#: a second competing classifier) - this is a bare STATE echo (what did I
#: already say), symmetric to `CART_STATUS`'s own bare cart-contents echo,
#: never a ranking/comparison/explanation question about products.
_PREFERENCE_STATUS_WORDS = (
    "my preference", "my preferences", "my priorities", "what have i told you",
    "what did i say i want", "what have i said i want", "what do you know about my",
)


def looks_like_preference_status_request(message: str) -> bool:
    """True when `message` itself contains a real, closed-class "what have
    I stated so far" phrase - a bare echo of `new_state`'s own real,
    already-persisted fields, never a guess at wording not already
    covered here."""
    lowered = f" {message.lower().strip()} "
    return any(word in lowered for word in _PREFERENCE_STATUS_WORDS)


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


#: Closed-class explicit-cart-verb vocabulary - same discipline as
#: `_COMPARISON_WORDS`/`_HISTORICAL_REFERENCE_WORDS`/`_RECOMMENDATION_WORDS`
#: above: a small, fixed, structurally reliable set of words that mean the
#: same commerce-action thing regardless of what else is in the sentence.
#: Live-reproduced gap this closes: "add both shoes and socks to my cart"
#: contains "both" - a real `looks_like_group_or_ordinal_reference` hit -
#: so `node_route_capability`'s own reference backstop (below) claimed
#: `Capability.COMPARE` for it BEFORE `resolve_cart_target` (the one
#: authoritative cart-intent check, per this module's own docstring) ever
#: got a chance to decide otherwise; when that separate, correct check
#: then (as it sometimes does, being its own LLM call with its own
#: variance) declined this exact multi-category phrasing, capability was
#: already irreversibly COMPARE, producing a dead-end "I don't have two
#: products in view to compare yet" reply to what was unambiguously a
#: cart-add command. This is never itself authorization for a cart
#: mutation (only `resolve_cart_target` is, unchanged) - it only stops the
#: reference backstop from stealing the turn away from that check, so an
#: explicit "add"/"buy"/"purchase" command always gets to fall through to
#: DISCOVER (never wrongly to COMPARE) if the cart check itself declines.
#:
#: Widened (2026-09, Phase 3/5 follow-up): originally add-only, which meant
#: "remove"/"clear" never reached the Rev. 2 single authoritative
#: interpretation call's own cart-resolution block (`node_route_capability`
#: below) at all - they fell straight through to `resolve_cart_target`'s
#: OWN, separate `interpret_cart_reference` call, a real second
#: interpretation authority for exactly the verbs that most need the
#: primary call's real conversational context (a bare "remove it"/"clear
#: my cart" has no content of its own beyond the verb - the SAME context
#: the single call already has and the separate call has to re-derive from
#: scratch). Closed-class, symmetric with "add"/"buy"/"purchase" - still
#: never itself authorization for a mutation; only decides which
#: resolution path a message reaches.
_EXPLICIT_CART_VERB_WORDS = ("add", "buy", "purchase", "remove", "clear", "empty")


def looks_like_explicit_cart_command(message: str) -> bool:
    """True when `message` itself contains a real, closed-class explicit
    cart-action verb - never itself cart-intent confirmation (that stays
    `resolve_cart_target`'s job alone); only used to withhold the
    reference backstop's own COMPARE override so that check keeps its
    first say."""
    lowered = f" {message.lower().strip()} "
    return any(f" {w} " in lowered for w in _EXPLICIT_CART_VERB_WORDS)


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


#: Deliberately broad, deliberately CHEAP - a real question mark, or a
#: message opening with a real interrogative/state-change word. NEVER the
#: decision about what the shopper meant (that is
#: `InterpretationResult.question_type`'s job, a genuine per-turn model
#: judgment) - purely a prefilter deciding whether the bounded
#: interpretation call is worth making at all, the exact same role
#: `looks_like_reference` already plays. False positives just cost one
#: extra bounded call; false negatives mean a genuine question gets no
#: interpretation attempt, which is the more expensive mistake - so this
#: stays intentionally generous rather than a tight per-phrase list.
_QUESTION_OPENERS = (
    "why", "what", "which", "how", "who", "when", "where", "compare",
    "no,", "no ", "never mind", "forget", "wait", "actually",
)


def looks_like_question(message: str) -> bool:
    """True when `message` is shaped like a question or a state-change
    utterance worth a real interpretation attempt - see this function's
    own module-level comment on why this stays broad."""
    stripped = message.strip().lower()
    if not stripped:
        return False
    if "?" in stripped:
        return True
    return any(stripped.startswith(w) for w in _QUESTION_OPENERS)


#: The correction/cancellation/pause-shaped subset of `_QUESTION_OPENERS`
#: - deliberately narrower than that full list (excludes plain
#: interrogatives like "why"/"what", which DO name real new questions and
#: should push a real topic when they do). Used ONLY to decide whether
#: `node_topic_transition` should defer creating a topic at all - never to
#: decide what the message MEANS (that stays `question_type`/
#: `conversational_repair`, a real per-turn model judgment). A message
#: that starts this way is overwhelmingly about something ALREADY said,
#: never a fresh product topic in its own right - pushing a new topic
#: labeled with the raw correction sentence itself ("Actually I meant the
#: shoes") is exactly the naturalness bug this exists to prevent.
_STATE_CHANGE_OPENERS = (
    "no,", "no ", "never mind", "forget", "wait", "actually",
)


def looks_like_state_change_shape(message: str) -> bool:
    """True when `message` opens with a real, closed-class correction/
    cancellation/pause marker - see this function's own module-level
    comment on scope and intent. Never sufficient alone to decide
    MEANING; only used to defer a topic push, same discipline
    `_has_new_mission_shape` already uses for its own deferral."""
    stripped = message.strip().lower()
    return any(stripped.startswith(w) for w in _STATE_CHANGE_OPENERS)
