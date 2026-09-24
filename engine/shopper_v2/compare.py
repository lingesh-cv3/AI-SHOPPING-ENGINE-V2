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

from engine.execution import ranking
from engine.execution.relevance import best_matches_with_score
from engine.reasoning.service import ReasoningService
from shared.models import Product

from .capability import (
    looks_like_comparison_request,
    looks_like_historical_reference,
    looks_like_recommendation_request,
)
from .evidence import TurnEvidence, already_verified_evidence
from .reference_resolution import (
    looks_like_comparative_pair_reference,
    parse_query_intent,
    resolve_comparative_pair,
    resolve_group_selector,
)
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
    group, never a guess at which one id represents the whole topic.

    Tie-break fix (2026-09, live-reproduced): when the message's only real
    matching word is generic ("shoe"), every shoe-related group in the
    ledger ties for the same score - a real, common case once a
    conversation has shown the same category more than once. Scanning
    `groups` MOST-RECENT-FIRST (it is stored oldest-first) and keeping the
    strict first winner means a tie is now resolved in favor of the group
    the shopper most recently saw, matching what "earlier" colloquially
    means in an ongoing conversation - not whichever matching group
    happened to be stored first."""
    groups = [g for g in state.recent_product_groups if g]
    if not groups:
        return []

    best_score = 0
    best_products: list[Product] = []
    for group_ids in reversed(groups):
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
    a real historical group - never a model guess, never invented.
    Consolidated onto the shared `engine.execution.ranking`/
    `reference_resolution.parse_query_intent` (the same ones
    `graph.py::node_discovery` uses) rather than this module's own,
    separate word list and min/max logic - see the root-cause
    consolidation this closes: a superlative used to only exist inside
    this one branch, unreachable from a bare discovery question with no
    historical-reference wording."""
    query_intent = parse_query_intent(message)
    if not query_intent.superlative or query_intent.sort_attribute not in ranking.KNOWN_ATTRIBUTES:
        return None
    pick = ranking.pick_superlative(products, query_intent.sort_attribute, query_intent.superlative)
    if pick is None:
        return None
    which = "cheapest" if query_intent.superlative == "min" else "most expensive"
    if query_intent.sort_attribute == "rating":
        which = "best rated" if query_intent.superlative == "max" else "lowest rated"
        return f"The {which} one I showed you was {pick.title}, rated {pick.rating}."
    return f"The {which} one I showed you was {pick.title}, at {pick.price}."


def _named_list(products: list[Product]) -> str:
    if len(products) == 1:
        return products[0].title
    return ", ".join(p.title for p in products[:-1]) + f" and {products[-1].title}"


def _resolve_named_subset(
    message: str, group_products: list[Product],
) -> tuple[list[Product], str] | None:
    """Real, structured resolution of WHICH real products in
    `group_products` (already in real display order) `message` names -
    "the first one", "both", "all three", "the first and third", or one
    side of a real comparative pair ("the cheaper one"). Never a phrase
    match against a specific example - see `reference_resolution.py`'s own
    docstring for why these markers generalize to any product/catalog.
    Returns `(products, why)` for the resolved real subset, or `None` when
    the message names no subset this can determine from real structure
    (the caller then falls back to the group as a whole)."""
    if looks_like_comparative_pair_reference(message):
        priced = [p for p in group_products if p.price is not None]
        pick = resolve_comparative_pair(message, priced)
        if pick is not None:
            return ([pick], f"the one you named is {pick.title}, at {pick.price}")
    positions = resolve_group_selector(message, len(group_products))
    if positions is not None:
        subset = [group_products[i] for i in positions]
        return (subset, f"you're referring to {_named_list(subset)}")
    return None


async def run_compare_turn(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    state: ShoppingStateV2,
) -> tuple[str, TurnEvidence, list[Product], bool]:
    """Returns (reply, evidence, fetched products, used_model)."""
    # Contextual reference resolution fix: a BARE comparative-pair
    # question ("which one was cheaper?", with no "earlier"/"before"
    # wording) means the products CURRENTLY in view - `active_products`,
    # this turn's own single topic - never a blend with a different,
    # earlier topic. Live-reproduced gap this closes: "show running
    # shoes" then "show socks" then "which one was cheaper" compared a
    # sock against a shoe, because the general merged-ids path below
    # backfills from history whenever the current topic alone is smaller
    # than its own minimum - correct for an EXPLICIT cross-topic ask
    # ("compare the shoes to the slides"), wrong for a bare pronoun that
    # named no second topic at all. Tried first, and only when the
    # message itself carries no historical-reference wording (that case
    # is handled by the historical-group branch below, which explicitly
    # reaches into the ledger on purpose).
    if looks_like_comparative_pair_reference(message) and not looks_like_historical_reference(
        message
    ):
        current_ids = list(dict.fromkeys(state.active_products))
        current_products: list[Product] = []
        for pid in current_ids:
            try:
                current_products.append(await adapter.get_product(pid))
            except Exception:  # noqa: BLE001 - degrade honestly, never raise
                logger.warning(
                    "shopper_v2 compare: current-topic get_product failed for %r",
                    pid, exc_info=True,
                )
        priced = [p for p in current_products if p.price is not None]
        if len(priced) == 2:
            pick = resolve_comparative_pair(message, priced)
            if pick is not None:
                evidence = already_verified_evidence(
                    priced, {p.product_id: ["shopper_named_comparison_target"] for p in priced},
                )
                return (f"That's {pick.title}, at {pick.price}.", evidence, priced, False)
        elif len(priced) < 2:
            # Genuinely nothing else currently in view to compare against -
            # say so honestly, naming what IS in view, rather than
            # silently reaching into a different, earlier topic the
            # shopper never asked to bring back in.
            named = f" I currently have {priced[0].title} in view." if priced else ""
            return (
                "I don't have two products from the same search in view to "
                f"compare yet.{named} Ask me to show something else first, "
                "or say 'from earlier' if you mean something I showed you before.",
                TurnEvidence(query=None, products=[]),
                current_products,
                False,
            )
        # More than 2 priced products currently in view - genuinely
        # ambiguous which pair "which one" means; falls through to the
        # general merged-ids path below rather than guessing a pair.

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
            evidence = already_verified_evidence(
                historical_products,
                {p.product_id: ["historical_group_reference"] for p in historical_products},
            )
            # Generalized structured resolution FIRST - "the second one you
            # showed me earlier", "both of the shoes from before", "the
            # cheaper one from that group" all name a specific real subset
            # of the historical group, not just its absolute superlative.
            named = _resolve_named_subset(message, historical_products)
            if named is not None:
                subset, why = named
                return (f"From what I showed you earlier - {why}.", evidence, subset, False)
            deterministic = _deterministic_superlative(message, historical_products)
            if deterministic:
                return (deterministic, evidence, historical_products, False)
            # A bare callback ("what about those shoes from earlier?") is
            # not itself asking for a comparison or analysis - it wants
            # the real group back, cleanly. Live-reproduced gap: routing
            # this through the same "comparison" model-synthesis prompt
            # every genuine compare/recommend question uses produced a
            # broad, vague reply, because there was no actual analytical
            # question in the message for the model to answer. Only a
            # message that ITSELF carries a real comparison word
            # ("compare"/"versus"/"which is better") reaches the model
            # synthesis path below; every other historical callback gets
            # this same deterministic, grounded re-listing - structural,
            # not phrase-specific (the same closed-class check
            # `capability.py`'s own routing already uses).
            if not looks_like_comparison_request(message):
                return (
                    f"Here's what I showed you earlier: {_named_list(historical_products)}.",
                    evidence, historical_products, False,
                )
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
    # latest topic). Widened from a hard cap of 2 - "all three"/"the first
    # and third" genuinely need the FULL currently-relevant group to
    # resolve against; the fixed-at-2 cap made those structurally
    # unresolvable regardless of what the shopper actually said.
    ids = recent_relevant_product_ids(state, minimum=2, cap=10)
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

    # Generalized structured resolution: a named subset ("both", "all
    # three", "the first and third", "the cheaper one") narrows to exactly
    # those real products before anything else runs - never the default
    # top-2, and never a guess when the count named doesn't match reality
    # (see `resolve_group_selector`'s own docstring).
    named = _resolve_named_subset(message, products)
    if named is not None:
        subset, _why = named
        if len(subset) == 1:
            evidence = already_verified_evidence(
                subset, {p.product_id: ["shopper_named_comparison_target"] for p in subset},
            )
            return (f"That's {subset[0].title}, at {subset[0].price}.", evidence, subset, False)
        products = subset
    else:
        products = products[:2]

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
