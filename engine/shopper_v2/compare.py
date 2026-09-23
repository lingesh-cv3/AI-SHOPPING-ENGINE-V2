"""Phase 3, first slice: Comparison.

Reads `ShoppingStateV2.active_products` (populated by `orchestrator.py`
after a discovery turn - see its own comment) to resolve "compare the top
two" without needing the model's discarded `ProposedAction` parameters.
Both products are ALWAYS fetched fresh via `adapter.get_product` - never
recalled from anything cached earlier in the conversation - mirroring V1's
own COMPARE_PRODUCTS guarantee (Completed.md #27) exactly, reused rather
than re-derived.

Read-only: calls only `adapter.get_product` (the same
`StandardCommerceInterface` read method V1's `COMPARE_PRODUCTS` already
uses) and `ReasoningService.synthesize(kind="comparison", ...)` (the same
existing, narrow, tool-free synthesis method `respond.py` already uses for
discovery). Never imports `engine.decision`, `engine.risk`, or any
`engine.execution` mutation path.
"""

from __future__ import annotations

import logging

from engine.execution.relevance import best_matches_with_score
from engine.reasoning.service import ReasoningService
from shared.models import Product

from .capability import looks_like_historical_reference, looks_like_recommendation_request
from .evidence import TurnEvidence, already_verified_evidence
from .respond import _claims_a_cart_action
from .state import ShoppingStateV2, recent_relevant_product_ids

logger = logging.getLogger(__name__)

_NEED_TWO_REPLY = (
    "I don't have two products in view to compare yet - tell me what you're "
    "looking for first, and I can compare the top options once you've seen some."
)
_FETCH_FAILED_REPLY = (
    "I couldn't fetch both of those products to compare them right now - "
    "please try again in a moment."
)

#: Closed-class superlative vocabulary - the same discipline
#: `capability.py`'s own comparison/temporal-reference word lists use.
#: When present, the answer is computed deterministically from real,
#: freshly-fetched price data (never a model guess at "the cheapest one")
#: - grounded the same way `mission_ranking.py`'s budget hard-filter
#: already is.
_CHEAPEST_WORDS = ("cheapest", "least expensive", "lowest price", "lowest priced")
_PRICIEST_WORDS = ("most expensive", "priciest", "highest price", "highest priced")


async def _best_matching_historical_group(
    message: str, state: ShoppingStateV2, adapter
) -> list[Product]:
    """Real, live-reproduced gap this closes: "what was the cheapest shoe
    you showed me earlier" after a topic switch (shoes -> socks) used to
    answer from whatever single id `recent_relevant_product_ids`'s
    minimum-based walk happened to grab first - never the actual, full,
    matching historical group. This fetches every distinct group still in
    `state.recent_product_groups` (bounded, already capped at
    `_LEDGER_MAX_GROUPS`), scores each group's own real, freshly-fetched
    products against the message using the SAME word-overlap scoring
    `relevance.py` already uses for ordinary discovery, and returns the
    full membership of whichever group scores highest - never a partial
    group, never a guess at which one id represents the whole topic."""
    groups = [g for g in state.recent_product_groups if g]
    if not groups:
        return []

    best_score = 0
    best_products: list[Product] = []
    for group_ids in groups:
        products: list[Product] = []
        for pid in group_ids:
            try:
                products.append(await adapter.get_product(pid))
            except Exception:  # noqa: BLE001 - skip what can't be fetched, never guess
                logger.warning(
                    "shopper_v2 compare: historical group get_product failed for %r",
                    pid, exc_info=True,
                )
        if not products:
            continue
        score, _ = best_matches_with_score(products, message)
        if score > best_score:
            best_score = score
            best_products = products
    return best_products


def _deterministic_superlative(message: str, products: list[Product]) -> str | None:
    """Real, grounded answer for a closed-class superlative question over
    a real historical group - never a model guess, never invented. Only
    ever considers products with a real, known price."""
    lowered = message.lower()
    priced = [p for p in products if p.price is not None]
    if not priced:
        return None
    if any(w in lowered for w in _CHEAPEST_WORDS):
        pick = min(priced, key=lambda p: p.price.amount)
        return f"The cheapest one I showed you was {pick.title}, at {pick.price}."
    if any(w in lowered for w in _PRICIEST_WORDS):
        pick = max(priced, key=lambda p: p.price.amount)
        return f"The most expensive one I showed you was {pick.title}, at {pick.price}."
    return None


async def run_compare_turn(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    state: ShoppingStateV2,
) -> tuple[str, TurnEvidence, list[Product], bool]:
    """Returns (reply, evidence, fetched products, used_model)."""
    # Conversational-flow review fix: a genuine reference to a PAST group
    # ("what was the cheapest shoe you showed me earlier") is not a 2-item
    # comparison at all - it needs the FULL matching historical group, not
    # `recent_relevant_product_ids`'s capped-at-2 minimum walk, which only
    # ever grabbed enough ids to satisfy its own count, not the right
    # ones. Tried first, and only when it actually finds a real matching
    # group - falls through to the ordinary 2-item path otherwise, so a
    # genuine "compare both" is never affected by this branch.
    if looks_like_historical_reference(message):
        historical_products = await _best_matching_historical_group(message, state, adapter)
        if historical_products:
            deterministic = _deterministic_superlative(message, historical_products)
            evidence = already_verified_evidence(
                historical_products,
                {p.product_id: ["historical_group_reference"] for p in historical_products},
            )
            if deterministic:
                return (deterministic, evidence, historical_products, False)
            items = [
                {
                    "product_id": p.product_id,
                    "title": p.title,
                    "price": str(p.price) if p.price is not None else None,
                    "availability": str(p.availability),
                    "description": p.description,
                }
                for p in historical_products
            ]
            synthesis = await reasoning.synthesize(
                kind="comparison", message=message, items=items,
                shopping_state=state.as_prompt_dict(),
            )
            if synthesis.text and not _claims_a_cart_action(synthesis.text):
                return (synthesis.text, evidence, historical_products, synthesis.used_model)

    # Batch 2: resolves from current AND historical context - see
    # `state.recent_relevant_product_ids`'s own docstring for the real,
    # reported gap this closes ("compare the shoes to the slides" after a
    # topic switch, when `active_products` alone only ever holds the
    # latest topic).
    ids = recent_relevant_product_ids(state, minimum=2, cap=2)
    if len(ids) < 2:
        return (_NEED_TWO_REPLY, TurnEvidence(query=None, products=[]), [], False)

    products: list[Product] = []
    for pid in ids:
        try:
            products.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001 - degrade honestly, never raise to the shopper
            logger.warning("shopper_v2 compare: get_product failed for %r", pid, exc_info=True)

    if len(products) < 2:
        return (_FETCH_FAILED_REPLY, TurnEvidence(query=None, products=[]), products, False)

    # Every fetched product is real, by construction - `exists=True` for
    # both, unconditionally verified relevant (a shopper-named comparison
    # target is not a discovery guess to re-score against a query). Shared
    # contract with `tradeoff.py` - see `evidence.already_verified_evidence`'s
    # own docstring for why this is not `build_evidence` itself.
    evidence = already_verified_evidence(
        products,
        {p.product_id: ["shopper_named_comparison_target"] for p in products},
    )

    items = [
        {
            "product_id": p.product_id,
            "title": p.title,
            "price": str(p.price) if p.price is not None else None,
            "availability": str(p.availability),
            "description": p.description,
        }
        for p in products
    ]
    # "What do you recommend?" after a comparison is a distinct request
    # from the comparison itself - a decisive pick, not another neutral
    # side-by-side description (live-reproduced gap: synthesis kept
    # describing both products as "each serving a different purpose"
    # rather than answering the actual question asked). Same real items,
    # same real constraints - only the synthesis framing changes; see
    # `_synthesis_prompt`'s own "recommendation" branch.
    synthesis_kind = (
        "recommendation" if looks_like_recommendation_request(message) else "comparison"
    )
    synthesis = await reasoning.synthesize(
        kind=synthesis_kind,
        message=message,
        items=items,
        shopping_state=state.as_prompt_dict(),
    )
    if synthesis.text and not _claims_a_cart_action(synthesis.text):
        return (synthesis.text, evidence, products, synthesis.used_model)

    return (_deterministic_comparison(products), evidence, products, False)


def _deterministic_comparison(products: list[Product]) -> str:
    a, b = products[0], products[1]
    parts = [f"{a.title}" + (f" ({a.price})" if a.price else "")]
    parts.append(f"{b.title}" + (f" ({b.price})" if b.price else ""))
    return "Here are both, fetched just now: " + " vs. ".join(parts) + "."
