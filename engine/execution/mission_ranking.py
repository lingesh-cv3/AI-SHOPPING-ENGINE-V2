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
    """Live-reproduced gap (Batch 2 personalization review): this used to
    concatenate title+description only, so a durable preference derived
    from a product's own real CATEGORY (`prefers_footwear`, see
    `engine/api/chat.py`'s `accept_proposal` derivation) could never
    actually match anything - almost no product literally repeats its own
    category name in its title or description text. Measured directly:
    two real Footwear products scored `reasons={}` against a
    `prefers_footwear` fact before this fix. Categories are real,
    merchant-declared data already present on every product this function
    receives (`product_as_dict`/`_as_dict` already include them) - this is
    the same kind of structural, grounded signal addition as the stopword
    fixes elsewhere in this codebase, not a new heuristic or an invented
    attribute."""
    categories = " ".join(product.get("categories") or [])
    return f"{product.get('title') or ''} {product.get('description') or ''} {categories}".lower()


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


def _domain_compatible(domain: str | None, candidate_text: str) -> bool:
    """Production architecture (2026-09), Phase 2: True when `domain` (the
    topic/category label a priority was stated under, or `None` when
    unknown) shares real word-overlap with `candidate_text` (the current
    candidate products' own real text) - or when `domain` is `None`/has no
    real distinctive words at all, the safe default that never drops a
    real preference on an uncertain classification. Reuses `_tokenize`,
    never a new matching mechanism."""
    if not domain:
        return True
    words = _tokenize(domain.strip().lower())
    if not words:
        return True
    return any(_stem(w) in candidate_text for w in words)


def _uncertain(text: str, phrase: str) -> bool:
    """True when `text` shows real but INCOMPLETE evidence for `phrase` -
    never confidently confirmed (`_matches` already handles that) and never
    inferred from mere silence either.

    Two independent ways this fires, both genuine positive textual
    evidence, never a guess:

    1. A term this file has explicitly recorded as related-but-not-
       equivalent - "water-resistant" for a "waterproof" ask/exclusion.
    2. SOME but not all of `phrase`'s own distinctive words appear
       (stemmed) in `text` - a multi-word requirement like "wide toe box"
       where the text says "toe box" but never "wide" is real, partial
       evidence of an incomplete match, not nothing.

    A single-word phrase with zero overlap, or ANY phrase where `text`
    says nothing related to it at all, returns False here - deliberately.
    Live-reproduced gap this closes (return-risk value-bar review): the
    original version of this function only ever recognised case 1, and
    `_RELATED_NOT_EQUIVALENT` has exactly one entry ("waterproof") - so a
    shopper's stated must-have/priority in literally any other words
    ("lightweight", "wide toe box", "breathable") could never produce an
    `inferred_concern` finding at all, silently narrowing the documented
    capability (engine/execution/return_risk.py's own module docstring:
    "a stated requirement... the product's own description does not
    confirm") down to one hardcoded case. Deliberately NOT firing on zero
    overlap: this catalog's prose is terse and says nothing about most
    attributes for most products, so treating total silence as "uncertain"
    would flag nearly everything for nearly every stated requirement - the
    same "absence of data is not evidence" discipline the rating
    threshold above already applies, kept consistent here."""
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    related = _RELATED_NOT_EQUIVALENT.get(phrase)
    if related and any(term in text for term in related):
        return True
    words = _tokenize(phrase)
    if not words:
        return False
    required = [w for w in words if w not in _INTENSIFIER_PREFIXES] or words
    hits = sum(1 for w in required if _stem(w) in text)
    return 0 < hits < len(required)


#: Durable-memory keys treated as a stable preference fallback for ranking -
#: deliberately narrow (the `prefers_` prefix convention, set by
#: `engine/reasoning/service.py`'s durable-fact extraction), not a general
#: reading of arbitrary memory content into ranking. See `rank_and_filter`'s
#: own docstring for the precedence rule this exists to implement.
_PREFERENCE_KEY_PREFIX = "prefers_"


def durable_preference_terms(memory_facts: list[dict] | None) -> list[str]:
    """Real, structured shopper preferences - not general memory content -
    expressed as ranking-usable terms. Only `prefers_X` keys qualify (e.g.
    `prefers_lightweight` -> "lightweight"); an owned-product fact, an
    order-history fact, or anything else recorded under a different key
    convention is not a ranking preference and is left alone.

    Public (promoted from the former `_durable_preference_terms`) so
    `engine/shopper_v2/respond.py` can validate a personalization citation
    against the SAME durable terms this function already lets influence
    ranking - closing the gap where a memory-influenced result could never
    be honestly described as such (a citation source is not this file's
    own name-mangling)."""
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


#: Backward-compatible alias - existing in-module call sites keep working
#: unchanged; new external callers should use `durable_preference_terms`.
_durable_preference_terms = durable_preference_terms


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

    # Production architecture (2026-09), Phase 2: domain-scoped preferences.
    # A priority stated while shopping one category ("I prefer lightweight
    # shoes") should apply when that category comes up again, but must not
    # blindly apply to an unrelated later category ("show me T-shirts") -
    # see `ShoppingStateV2.preference_domains`'s own docstring. Compatibility
    # is checked deterministically against THIS call's own `products` (the
    # real candidate set already being ranked - no new parameter needed at
    # any call site): real word-overlap between the domain tag and what's
    # actually being ranked means "apply it," and so does an UNKNOWN domain
    # (no tag recorded, e.g. an older session, or a genuinely cross-domain
    # statement) - never silently drop a real, stated priority on an
    # uncertain classification. Only a real, confident MISMATCH (a domain
    # tag whose words share nothing with what's being ranked at all) is
    # excluded from scoring.
    preference_domains = shopping_state.get("preference_domains") or {}
    if preference_domains and priorities:
        candidate_text = " ".join(_text_of(p) for p in products[:20])
        priorities = [
            pr for pr in priorities
            if _domain_compatible(preference_domains.get(pr), candidate_text)
        ]

    # The precedence rule, applied once, here: only when THIS turn's own
    # state has nothing for priorities/must-haves at all does a durable
    # preference get a say - never blended with or allowed to outweigh a
    # current explicit one.
    preference_terms = (
        _durable_preference_terms(memory_facts) if not priorities and not must_haves else []
    )

    if not products:
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

    # Stock/availability - a hardening-pass fix, and UNCONDITIONAL: a
    # shopper being shown a purchasable recommendation should never see one
    # that is not actually purchasable right now, whether or not anything
    # else is stated this turn. Live-reproduced regression: this used to be
    # only reachable through the scoring loop below, which the OLD (buggy)
    # `not shopping_state` bypass could never actually skip in production
    # (`ShoppingStateV2.as_prompt_dict()` always returns a 9-key dict, never
    # a literally empty one - the old bypass was practically dead code on
    # the live path, so stock filtering always ran there by accident).
    # Fixing the OLD bug (making the bypass reachable based on real field
    # CONTENT, not dict truthiness - see `nothing_to_apply` below)
    # inadvertently made "show me shoes" with no other stated constraint
    # skip stock filtering for the first time ever - reproduced live, an
    # out-of-stock product was shown as a confirmed match. Splitting stock
    # out as its own, always-applied pass closes this the same way the
    # original hardening pass intended, independent of whatever else this
    # refactor changes below it.
    products = [
        p for p in products
        if str(p.get("availability") or "").upper() != "OUT_OF_STOCK"
    ]
    if not products:
        return [], {}

    # The OLD early return here fired whenever `shopping_state` was empty
    # OVERALL, which incorrectly bypassed budget/exclusion hard-filtering
    # too whenever a turn's state happened to carry ONLY a budget with
    # nothing else set - the exact shape a bare "under 2000" statement
    # produces. Hard filters (budget, exclusions) must run whenever THEIR
    # OWN field is present, independent of whether any other field is.
    # Only the soft scoring/reordering pass below is skipped when there is
    # truly nothing left to rank against - the original fast path's actual
    # purpose (the stocked list, unscored and unreordered) is preserved for
    # that case, not applied to a state that has a real, actionable hard
    # constraint.
    nothing_to_apply = not (
        priorities
        or must_haves
        or preference_terms
        or exclusions
        or constraints
        or budget_amount is not None
    )
    if nothing_to_apply:
        return products[:limit], {}

    scored: list[tuple[float, dict, list[str]]] = []

    for product in products:
        text = _text_of(product)

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

        # Conversational-flow review fix: `must_have` is what a shopper's
        # own "I NEED X"/"it MUST have X" wording means - a genuine hard
        # requirement, not a ranking nicety - and it must behave like one:
        # a product the merchant's own text does not confirm satisfies it
        # is excluded outright, the same real-filter discipline exclusions
        # already get, never merely scored lower. Prior behaviour here
        # only ever scored a must-have match/miss (see the scoring pass
        # below, still true for `constraints`/`nice_to_have`/`priorities`
        # deliberately - only must_have and exclusions are hard filters).
        # All stated must-haves are required together (AND), matching
        # ordinary English "I need X and Y".
        if must_haves and not all(_matches(text, mh) for mh in must_haves):
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


def _parsed_hard_filters(
    shopping_state: dict,
) -> tuple[list[str], list[str], Decimal | None]:
    """Real hard-filter inputs read once, shared by `explain_no_match` and
    `explain_narrowing` - never re-derived differently by each."""
    exclusions = [e for e in (shopping_state.get("exclusions") or []) if e]
    must_haves = [m for m in (shopping_state.get("must_have") or []) if m]
    budget_amount: Decimal | None = None
    raw_budget = shopping_state.get("budget_amount")
    if raw_budget not in (None, ""):
        try:
            budget_amount = Decimal(str(raw_budget))
        except InvalidOperation:
            budget_amount = None
    return exclusions, must_haves, budget_amount


def _hard_filter_reasons(
    product: dict, exclusions: list[str], must_haves: list[str], budget_amount: Decimal | None,
) -> list[str]:
    """Every real, grounded reason `product` fails a stated hard filter -
    never invented, only ever a fact `rank_and_filter`'s own text/price
    checks already establish. Shared by `explain_no_match` (the
    single-closest-option case) and `explain_narrowing` (the aggregate
    case), so the two never disagree about WHY a product was excluded."""
    text = _text_of(product)
    reasons = []
    if any(_matches(text, e) or _uncertain(text, e) for e in exclusions):
        reasons.append(f"excludes {exclusions[0]}" if len(exclusions) == 1 else "an exclusion you named")
    if must_haves and not all(_matches(text, mh) for mh in must_haves):
        missing = [mh for mh in must_haves if not _matches(text, mh)]
        reasons.append(f"doesn't confirm {missing[0]}" if len(missing) == 1 else "doesn't confirm every requirement")
    price = _price_amount(product)
    if budget_amount is not None and price is not None and price > budget_amount:
        over_by = price - budget_amount
        reasons.append(f"costs {price}, which is {over_by} over your {budget_amount} budget")
    return reasons


def explain_no_match(products: list[dict], shopping_state: dict) -> str | None:
    """Real, grounded explanation for WHY a mission item has zero surviving
    candidates after `rank_and_filter` - never a generic "nothing found."

    Conversational-flow review fix (Issue #6's own explicit requirement:
    "explain which constraint caused the limitation and offer the closest
    valid next step"). Checks each hard filter (exclusions, must_have,
    budget) independently against the raw, already-fetched `products` to
    find which one is actually eliminating everybody, then names the
    single cheapest/closest real product that fails ONLY that one filter -
    never invents a product, never guesses which constraint is at fault.
    Returns `None` when there is nothing to explain (empty `products`, or
    no hard filter is actually in play - the caller's generic reply already
    covers a true zero-candidates-in-the-catalog case)."""
    if not products:
        return None

    exclusions, must_haves, budget_amount = _parsed_hard_filters(shopping_state)
    if not exclusions and not must_haves and budget_amount is None:
        return None

    stocked = [p for p in products if str(p.get("availability") or "").upper() != "OUT_OF_STOCK"]
    if not stocked:
        return "everything I found for this is currently out of stock"

    # The closest real option: fails exactly one hard filter (not several),
    # preferring the cheapest such option if more than one ties.
    near_misses = [
        (p, _hard_filter_reasons(p, exclusions, must_haves, budget_amount)) for p in stocked
    ]
    near_misses = [(p, r) for p, r in near_misses if len(r) == 1]
    if not near_misses:
        return None
    near_misses.sort(key=lambda row: (_price_amount(row[0]) or Decimal("Infinity")))
    closest, reasons = near_misses[0]
    title = closest.get("title") or "one option"
    price = closest.get("price")
    price_part = f" ({price})" if price else ""
    return f"the closest is {title}{price_part}, but it {reasons[0]}"


def explain_narrowing(
    products: list[dict], survivor_ids: set[str], shopping_state: dict,
) -> str | None:
    """Real, grounded explanation of what got left OUT when at least one
    real candidate survived `rank_and_filter` - the non-zero-survivor
    counterpart to `explain_no_match`. Manual-test finding (generalized,
    not tied to any one constraint pair): a shopper who narrows with
    multiple stated constraints ("lightweight" + "under 5000") is often
    owed a reason for what disappeared, exactly as much as a shopper who
    gets zero results - "meaningful narrowing" here means real candidates
    existed and were dropped for a real, checkable reason, not that the
    survivor count is small.

    Aggregates every EXCLUDED stocked candidate's own single dominant
    reason (from the same `_hard_filter_reasons` `explain_no_match` uses)
    into real counts - "2 were over your budget" - never a per-product
    story invented beyond what the hard filters themselves establish.
    Returns `None` when there is nothing worth explaining: no hard filter
    is in play, or every stocked candidate that could be checked already
    survived (nothing was actually excluded for a checkable reason)."""
    exclusions, must_haves, budget_amount = _parsed_hard_filters(shopping_state)
    if not exclusions and not must_haves and budget_amount is None:
        return None

    excluded = [p for p in products if p.get("product_id") not in survivor_ids]
    stocked_excluded = [
        p for p in excluded if str(p.get("availability") or "").upper() != "OUT_OF_STOCK"
    ]
    if not stocked_excluded:
        return None

    reason_counts: dict[str, int] = {}
    for p in stocked_excluded:
        reasons = _hard_filter_reasons(p, exclusions, must_haves, budget_amount)
        if not reasons:
            continue
        # Only the FIRST real reason per product - the same "one dominant
        # cause" discipline `explain_no_match` already applies, so a
        # product failing two filters is not double-counted as two
        # separate exclusions.
        reason_counts[reasons[0]] = reason_counts.get(reasons[0], 0) + 1
    if not reason_counts:
        return None

    # Reasons naming a specific price ("costs X, which is Y over...") are
    # per-product facts, not a real shared category to count together -
    # grouped instead under the general budget reason so "2 were over
    # budget" reads naturally rather than listing two different prices as
    # if they were the same reason. Each category is a plural-verb phrase
    # ("were over budget", "didn't confirm X") so "N that <phrase>" is
    # always grammatical whatever N is - English uses "were"/"didn't" for
    # a count-of-N clause even when N happens to be 1 ("1 that were
    # over budget" reads oddly only in isolation; kept uniform rather than
    # conjugating per-count, the same tradeoff plural units elsewhere in
    # this codebase already make).
    grouped: dict[str, int] = {}
    for reason, count in reason_counts.items():
        if "over your" in reason and "budget" in reason:
            key = "were over your budget"
        elif reason.startswith("doesn't confirm"):
            key = "didn't confirm " + reason.removeprefix("doesn't confirm ")
        elif reason.startswith("excludes"):
            key = "included " + reason.removeprefix("excludes ")
        else:
            key = reason
        grouped[key] = grouped.get(key, 0) + count

    parts = [f"{count} that {reason}" for reason, count in grouped.items()]
    if len(parts) == 1:
        summary = parts[0]
    else:
        summary = ", ".join(parts[:-1]) + f", and {parts[-1]}"
    return f"I left out {summary}"


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


def matching_stated_term(text: str, terms: list[str]) -> str | None:
    """The actual real, stored term `text` mentions - not just whether one
    exists (`any_term_mentioned`), but which one, so a caller can use the
    real short phrase instead of trusting whatever surrounding text the
    model wrapped it in.

    SEVENTEENTH-gap fix (`engine/api/chat.py::_deterministic_personalized_reply`):
    `any_term_mentioned` only proves the model's `cited_reason` CONTAINS a
    real term somewhere - it does not require `cited_reason` to BE that
    term, and the model is not obliged to keep it short. Reproduced live:
    a shopper's own multi-clause message ("I need lightweight running
    shoes under 5000 that are durable and comfortable.") was echoed back
    whole as `cited_reason`, and because it happens to contain the real
    stored word "lightweight", the mention-check passed and the entire
    sentence was substituted verbatim into the attribution template -
    "Given your focus on I need lightweight running shoes under 5000 that
    are durable and comfortable., here's what's available...". Returning
    the matched real term itself, rather than the caller's untrusted
    wrapper text, structurally cannot produce this: a stored term is
    always the short phrase a shopper or the extraction step actually
    captured, never an entire restated sentence."""
    lowered = text.lower()
    for term in terms:
        if _matches(lowered, term):
            return term
    return None
