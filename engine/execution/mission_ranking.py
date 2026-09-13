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
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

#: How many candidates to keep. A shortlist, not a catalog page - CLAUDE.md's
#: own research basis names 74% of shoppers wanting comparison and shoppers
#: abandoning after one irrelevant suggestion; a small, justified set serves
#: that better than the six-arbitrary-products shape Phase 1 inherited.
DEFAULT_LIMIT = 4


def _text_of(product: dict) -> str:
    return f"{product.get('title', '')} {product.get('description', '')}".lower()


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


def _matches(text: str, phrase: str) -> bool:
    """A coarse, honest keyword check - not a claim the catalog has a
    structured attribute for this. Every distinctive word (len > 3) in
    `phrase` must have its stem appear in `text`: "cushioning" matches
    "cushioned midsole" via the shared "cushio" root; an exclusion phrase
    like "not waterproof" matches a description that says "waterproof",
    because the word itself is the only place this catalog ever records
    such a fact."""
    phrase = phrase.strip().lower()
    if not phrase:
        return False
    words = [w for w in phrase.split() if len(w) > 3]
    if not words:
        return phrase in text
    return all(_stem(w) in text for w in words)


def rank_and_filter(
    products: list[dict],
    shopping_state: dict | None,
    *,
    limit: int = DEFAULT_LIMIT,
) -> tuple[list[dict], dict[str, list[str]]]:
    """Narrow and order `products` against the shopper's current mission.

    Returns (shortlist, fit_reasons). `fit_reasons` maps product_id to a
    short list of plain-English reasons, each grounded in a real field
    (price, description text, rating) - never an invented one.

    With no shopping_state at all, returns the first `limit` products
    unchanged and no reasons: there is no mission to reason against yet, and
    inventing evidence for one would be exactly the defect this mechanism
    exists to prevent.

    Can legitimately return an empty shortlist from a non-empty input - a
    genuinely full exclusion/budget match. The caller is expected to say so
    honestly rather than treat it as "nothing in the catalog" (see
    engine/api/chat.py's RECOMMEND_PRODUCTS branch).
    """
    if not shopping_state or not products:
        return products[:limit], {}

    exclusions = [e for e in (shopping_state.get("exclusions") or []) if e]
    must_haves = [m for m in (shopping_state.get("must_have") or []) if m]
    # Recency-weighted: the most recently stated priority (last in the list)
    # outweighs an earlier one. This is what makes "actually cushioning
    # matters more than weight" change the ranking itself, not just the
    # prose describing it - not NLP contradiction detection, just "the
    # newest explicit priority carries more weight," expressed numerically.
    priorities = [p for p in (shopping_state.get("priorities") or []) if p]
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

        # The one hard filter.
        if any(_matches(text, excl) for excl in exclusions):
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

        for i, pr in enumerate(priorities):
            if _matches(text, pr):
                score += 1.0 + i * 0.5
                why.append(f"fits your priority: {pr}")

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
