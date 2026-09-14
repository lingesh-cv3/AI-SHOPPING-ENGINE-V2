"""Deterministic, mission-aware filtering and ranking over already-fetched,
authoritative product data - never a model call, never an invented attribute.

Applied once in engine/api/chat.py, to the real products a RECOMMEND_PRODUCTS
turn already fetched via the adapter. Narrows a catalog-order/keyword-order
list into a small, evidence-backed shortlist that actually reflects the
shopper's stated mission (engine/db/shopping_state.py), rather than an
arbitrary first-N slice - this is the deterministic half of Phase 2's
discovery/shortlist work; the model's job stays limited to writing the
narrative synthesis over whatever this function has already decided to show
(engine/reasoning/service.py::synthesize), never to picking the set itself.

Exclusions are the only hard filter: a shopper who said "not waterproof"
must never see a waterproof product ranked in, however well it otherwise
scores. Everything else - must-haves, priorities, budget, rating - is a
soft, additive signal. A product missing a must-have keyword is not
evidence it lacks the feature (this catalog has no structured attribute for
most of these - "lightweight", "cushioned" are prose, not fields), only
that the merchant's own title/description does not say so, so it is scored
lower rather than dropped outright. Nothing here fabricates an attribute
the catalog does not provide.

Reads only `shopping_state` (the current mission) - never durable shopper
memory. That is deliberate, not an oversight: it is what makes "current
explicit intent beats older stored preference" a structural guarantee
rather than a prompt request. Durable memory is still allowed to colour the
model's narrative synthesis (see engine/reasoning/context.py), but it can
never move a product up or down this list.

Hardening pass, on top of the independent Phase 1+2 audit (see PROGRESS.md):
fixed a dead `constraints` field (now read, scored the same as must-haves -
see the module docstring's note below on why they share treatment), a
hyphenated/intensifier matching gap ("ultra-lightweight" never matching
"lightweight" prose), an unsafe "related-but-not-equivalent" false
compliance (an exclusion like "waterproof" silently passing a product
described only as "water-resistant"), and a missing stock/availability
check (an out-of-stock product could be shortlisted with no penalty).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: How many candidates to keep. A shortlist, not a catalog page - CLAUDE.md's
#: own research basis names 74% of shoppers wanting comparison and shoppers
#: abandoning after one irrelevant suggestion; a small, justified set serves
#: that better than the six-arbitrary-products shape Phase 1 inherited.
DEFAULT_LIMIT = 4

#: Common intensifier prefixes, hyphen- or space-joined ("ultra-lightweight",
#: "super light", "max cushioned"). Dropped from the required-match set
#: rather than stemmed and required, because they modify degree, not
#: identity - a product described as "lightweight" genuinely satisfies a
#: shopper who said "ultra-lightweight" even though the merchant's prose
#: never literally says "ultra". A concrete, deterministic, small list -
#: not a claim of general morphological understanding, and not used for
#: anything except deciding which of a phrase's own words must be found.
_INTENSIFIER_PREFIXES = {
    "ultra", "super", "extremely", "very", "extra", "hyper", "max", "maximum",
}

#: Explicit, narrow "related but not equivalent" table. A shopper excluding
#: "waterproof" is not safely satisfied by a product merely described as
#: "water-resistant"/"water repellent" - those are real, different claims in
#: outdoor-gear marketing, and treating them as the same would be inventing
#: an equivalence the merchant never stated. Rather than silently pass such
#: a product as compliant (the bug this table fixes) or silently exclude it
#: as if the words were identical (asserting a fact nobody stated either
#: way), a product matching only a *related* term is treated as UNCERTAIN -
#: see `_uncertain` below - and kept out of the confident shortlist rather
#: than shown as if the exclusion were confirmed satisfied. Deliberately
#: small and explicit rather than a general synonym/relatedness model: add
#: an entry here only for a real, confirmed case, the same way this file's
#: own history added the intensifier list above from a live-test finding.
_RELATED_NOT_EQUIVALENT: dict[str, tuple[str, ...]] = {
    "waterproof": ("water-resistant", "water resistant", "water repellent", "weather resistant"),
}


def _text_of(product: dict) -> str:
    return f"{product.get('title') or ''} {product.get('description') or ''}".lower()


def _price_amount(product: dict) -> Decimal | None:
    """The numeric amount out of a rendered price string like "4299.00 INR".
    None for anything that doesn't parse - never guessed at."""
    raw = product.get("price")
    if not raw:
        return None
    try:
        return Decimal(str(raw).split()[0])
    except (InvalidOperation, IndexError):
        return None


def _stem(word: str) -> str:
    """A deliberately crude stem, not a real stemmer: the first 6 characters
    of anything longer. Merchant prose inflects freely - "cushioned midsole"
    for a shopper who said "cushioning" - and a whole-word substring check
    missed that real pairing during live verification (a genuine live-test
    finding, not a hypothetical). Truncating to a shared root is honest
    about what it is (a coarse heuristic, not semantic understanding) while
    catching the common singular/plural and verb-form variations this
    catalog's prose actually uses."""
    return word if len(word) <= 6 else word[:6]


def _tokenize(phrase: str) -> list[str]:
    """Words worth requiring a match on, treating a hyphen exactly like a
    space. "ultra-lightweight" and "ultra lightweight" must behave
    identically - a live-test finding showed the hyphenated form silently
    matching nothing at all, because splitting on whitespace alone left it
    as one unstemmable token. "carbon-plate" and "carbon plate" likewise
    become the same two words either way."""
    return [w for w in phrase.replace("-", " ").split() if len(w) > 3]


def _matches(text: str, phrase: str) -> bool:
    """A coarse, honest keyword check - not a claim the catalog has a
    structured attribute for this. Every distinctive word in `phrase`
    (hyphens treated as spaces) must have its stem appear in `text`,
    **except** a known intensifier prefix, which is dropped from the
    requirement rather than demanded literally - "ultra-lightweight"
    matches "Lightweight daily trainer" via the shared "lightweight"/
    "lightw" root, without requiring the merchant's prose to say "ultra".
    "cushioning" matches "cushioned midsole" via the shared "cushio" root;
    an exclusion phrase like "waterproof" matches a description that says
    "waterproof", because the word itself is the only place this catalog
    ever records such a fact - see `_uncertain` for the different,
    related-but-not-confirmed case."""
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    words = _tokenize(phrase)
    if not words:
        return phrase.replace("-", " ") in text
    required = [w for w in words if w not in _INTENSIFIER_PREFIXES] or words
    return all(_stem(w) in text for w in required)


def _uncertain(text: str, exclusion_phrase: str) -> bool:
    """True when `text` doesn't confidently satisfy `exclusion_phrase` (does
    not literally match it) but does contain a term this file has explicitly
    recorded as related-but-not-equivalent - "water-resistant" for a
    "waterproof" exclusion. An uncertain product is neither confidently
    excluded (nobody claimed it IS waterproof) nor confidently kept (the
    merchant's own words raise a real question a stemmed keyword check
    cannot resolve safely) - see `rank_and_filter`'s own use of this."""
    related = _RELATED_NOT_EQUIVALENT.get(exclusion_phrase.strip().lower())
    if not related:
        return False
    return any(term in text for term in related)


#: Durable-memory keys treated as a stable preference fallback for ranking -
#: deliberately narrow (the `prefers_` prefix convention, set by
#: `engine/reasoning/service.py`'s durable-fact extraction), not a general
#: reading of arbitrary memory content into ranking. See `rank_and_filter`'s
#: own docstring for the precedence rule this exists to implement.
_PREFERENCE_KEY_PREFIX = "prefers_"


def _durable_preference_terms(memory_facts: list[dict] | None) -> list[str]:
    """Real, structured shopper preferences - not general memory content -
    expressed as ranking-usable terms. Only `prefers_X` keys qualify (e.g.
    `prefers_lightweight` -> "lightweight"); an owned-product fact, an
    order-history fact, or anything else recorded under a different key
    convention is not a ranking preference and is left alone."""
    if not memory_facts:
        return []
    terms = []
    for fact in memory_facts:
        key = str(fact.get("key", ""))
        if key.startswith(_PREFERENCE_KEY_PREFIX):
            term = key[len(_PREFERENCE_KEY_PREFIX):].replace("_", " ").strip()
            if term:
                terms.append(term)
    return terms


def rank_and_filter(
    products: list[dict],
    shopping_state: dict | None,
    *,
    memory_facts: list[dict] | None = None,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Narrow and order `products` against the shopper's current mission -
    and, deliberately and only as a fallback, their durable preferences.

    Returns (shortlist, fit_reasons). `fit_reasons` maps product_id to a
    short list of plain-English reasons, each grounded in a real field
    (price, description text, rating) - never an invented one.

    With no shopping_state and no usable `memory_facts` at all, returns the
    first `limit` products unchanged and no reasons: there is no mission to
    reason against yet, and inventing evidence for one would be exactly the
    defect this mechanism exists to prevent.

    **Deliberate personalization, not accidental prompt behaviour.** Prior
    to this, `memory_facts` was never passed to this function at all - a
    durable preference could only reach ranking by accident, if the model
    happened to fold it into the *current* `shopping_state.must_have` on
    its own initiative (a real, observed, but unreliable and unintentional
    side effect - see PROGRESS.md). This makes it a real, tested, code-level
    feature instead, with an explicit precedence rule: a durable preference
    (`prefers_X` facts, `_durable_preference_terms`) is only ever used to
    fill in for a dimension the CURRENT session has said nothing about -
    if `shopping_state` already has its own `priorities`/`must_have` for
    this turn, memory contributes nothing at all, whatever it contains.
    When it does apply, it scores lower than any current-turn signal (a
    flat, non-recency-weighted bonus, always less than a fresh must-have or
    priority) and is labelled in `fit_reasons` as coming from what the
    shopper has told this shop before, not from this message - so "current
    explicit intent beats stale memory" holds structurally, the same as the
    Phase 1 guarantee this extends rather than replaces.

    Can legitimately return an empty shortlist from a non-empty input - a
    genuinely full exclusion/budget match. The caller is expected to say so
    honestly rather than treat it as "nothing in the catalog" (see
    engine/api/chat.py's RECOMMEND_PRODUCTS branch).
    """
    shopping_state = shopping_state or {}
    priorities = [p for p in (shopping_state.get("priorities") or []) if p]
    must_haves = [m for m in (shopping_state.get("must_have") or []) if m]

    # The precedence rule, applied once, here: only when THIS turn's own
    # state has nothing for priorities/must-haves at all does a durable
    # preference get a say - never blended with or allowed to outweigh a
    # current explicit one.
    preference_terms = (
        _durable_preference_terms(memory_facts) if not priorities and not must_haves else []
    )

    if (not shopping_state and not preference_terms) or not products:
        return products[:limit], {}

    exclusions = [e for e in (shopping_state.get("exclusions") or []) if e]
    # `constraints` used to be captured by the model and never read here -
    # silently inert, a real bug the hardening pass fixed. A stated
    # constraint is a real shopper requirement whether or not the model
    # happened to file it under `must_have` instead; scored identically
    # (soft, additive) rather than invented a separate hard-filter meaning
    # for a field whose actual content is a mix of positive and negative
    # statements depending on what the model chose to put there - see
    # engine/reasoning/prompts.py's schema comment for the fuller account.
    constraints = [c for c in (shopping_state.get("constraints") or []) if c]
    # `priorities`/`must_haves` were already extracted above, before the
    # personalization-precedence check - not re-extracted here.
    #
    # Recency-weighted: the most recently stated priority (last in the list)
    # outweighs an earlier one. This is what makes "actually cushioning
    # matters more than weight" change the ranking itself, not just the
    # prose describing it - not NLP contradiction detection, just "the
    # newest explicit priority carries more weight," expressed numerically.
    budget_amount: Decimal | None = None
    raw_budget = shopping_state.get("budget_amount")
    if raw_budget:
        try:
            budget_amount = Decimal(str(raw_budget))
        except InvalidOperation:
            budget_amount = None

    scored: list[tuple[float, dict, list[str]]] = []

    for product in products:
        text = _text_of(product)

        # Stock/availability - a hardening-pass fix. A shopper being shown a
        # purchasable recommendation should never see one that is not
        # actually purchasable right now. Only a *confirmed* OUT_OF_STOCK
        # is dropped; missing/unknown availability is not invented as
        # either in-stock or out - it is simply not penalized, the same
        # "absence is not evidence" rule this file already applies to
        # missing must-have keywords.
        if str(product.get("availability") or "").upper() == "OUT_OF_STOCK":
            continue

        # The hard filters: an exclusion nobody confirmed the product
        # violates is not dropped; one the merchant's own words confirm is
        # always dropped, whatever else it scores. A product whose text
        # only raises a related-but-not-equivalent question (waterproof vs.
        # water-resistant) is ALSO kept out of the confident shortlist -
        # not because it's confirmed to violate the exclusion, but because
        # this file cannot safely confirm it does NOT, and showing it
        # unqualified would be exactly the "confidently claims an exclusion
        # was respected when the attribute is actually ambiguous" defect
        # the hardening pass exists to close.
        if any(_matches(text, excl) or _uncertain(text, excl) for excl in exclusions):
            continue

        price = _price_amount(product)
        if budget_amount is not None and price is not None and price > budget_amount:
            continue

        score = 0.0
        why: list[str] = []

        if budget_amount is not None and price is not None:
            score += 1.0
            why.append(f"within your budget ({product.get('price')})")

        for mh in must_haves:
            if _matches(text, mh):
                score += 1.0
                why.append(f"matches '{mh}'")

        for c in constraints:
            if _matches(text, c):
                score += 1.0
                why.append(f"meets your requirement: {c}")

        for i, pr in enumerate(priorities):
            if _matches(text, pr):
                score += 1.0 + i * 0.5
                why.append(f"fits your priority: {pr}")

        # Deliberate personalization - only ever populated when THIS turn
        # stated no priority/must-have of its own (see the precedence check
        # above `preference_terms` is derived from). Weighted below every
        # current-turn signal (0.4 < the 1.0 floor everything else uses) so
        # it can nudge a tiebreak but never outrank an explicit current
        # statement - which cannot coexist with a non-empty
        # `preference_terms` anyway, by construction.
        for pref in preference_terms:
            if _matches(text, pref):
                score += 0.4
                why.append(f"you've told us before you prefer {pref}")

        rating = product.get("rating")
        if isinstance(rating, (int, float)):
            score += rating / 10.0  # a tiebreaker, never the deciding factor

        scored.append((score, product, why))

    scored.sort(key=lambda row: row[0], reverse=True)
    shortlist = [p for _, p, _ in scored[:limit]]
    reasons = {
        p["product_id"]: why
        for _, p, why in scored[:limit]
        if p.get("product_id") and why
    }
    return shortlist, reasons


def has_stated_priority(shopping_state: dict | None) -> bool:
    """Whether `shopping_state` actually carries anything that could
    legitimately justify a narrative claim like "because you prioritized
    X" or "since X matters most to you" - i.e. `priorities`, `must_have`,
    or `constraints` is genuinely non-empty.

    This is the authoritative check `engine/api/chat.py` uses to decide
    whether the model's synthesized reply is allowed to make a
    priority-attribution claim at all - see its own docstring for the
    hardening-pass finding this exists to close: the model can write a
    fully convincing "since you prioritized carbon plate..." sentence in a
    turn where `priorities`/`must_have` are both empty, because it is
    reasoning from the raw shopper message rather than from what was
    actually captured and used for ranking. A narrative claim with nothing
    here to back it is not evidence the shopper's words were used - it is
    the model re-reading its own copy of the same words.
    """
    if not shopping_state:
        return False
    return bool(
        shopping_state.get("priorities")
        or shopping_state.get("must_have")
        or shopping_state.get("constraints")
    )


def stated_terms(shopping_state: dict | None) -> list[str]:
    """The actual priority/must-have/constraint phrases captured this
    session - not just whether any exist (`has_stated_priority`), but what
    they specifically are. Post-hardening-pass finding, found by further
    adversarial testing of `_honest_synthesis`: checking only "is
    `shopping_state` non-empty" let a synthesized reply through that
    attributed the recommendation to a priority the shopper never actually
    stated (state had `priorities=["lightweight"]`; the text confidently
    claimed "since you prioritized durability...") - any grounded state at
    all was enough to wave the whole reply through, whatever it specifically
    claimed. `engine/api/chat.py::_honest_synthesis` uses this list to also
    verify the claim is *about* something actually stored, not merely that
    something, anything, was stored.
    """
    if not shopping_state:
        return []
    return [
        *(shopping_state.get("priorities") or []),
        *(shopping_state.get("must_have") or []),
        *(shopping_state.get("constraints") or []),
    ]


def any_term_mentioned(text: str, terms: list[str]) -> bool:
    """Whether `text` actually mentions at least one of `terms` - the same
    coarse, honest stem/hyphen-aware keyword check `_matches` already uses
    for ranking, reused here for a different purpose: verifying a
    synthesized claim is *about* a real, stored term rather than an
    unrelated one. Not a semantic entailment check (this file makes no
    claim to that) - only a floor: a reply naming zero of the shopper's own
    stated terms anywhere is not a reply about what the shopper said."""
    if not terms:
        return False
    lowered = text.lower()
    return any(_matches(lowered, term) for term in terms)
