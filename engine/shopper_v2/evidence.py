"""The Evidence object: wraps the EXISTING deterministic relevance/ranking
logic (`engine.execution.relevance`, `engine.execution.mission_ranking`)
behind named fields, per the Stage 1 spec. No new scoring heuristic is
introduced here - every verdict below is produced by calling an existing,
already-hardened function, not by re-deriving its logic.

This module is the direct, structural answer to "product exists != product
is relevant": a `ProductEvidence.exists=True` entry is never enough on its
own for `respond.py` to describe a product as a match - `relevance_verified`
must also be True.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from engine.execution import mission_ranking
from engine.execution.relevance import (
    best_matches_with_score,
    query_diverges_from_said,
    shop_carries,
    weak_single_word_match,
)
from shared.models import Product


class ProductEvidence(BaseModel):
    product_id: str
    exists: bool
    category_match: bool | None = None
    hard_constraints_passed: bool
    failed_constraints: list[str] = Field(default_factory=list)
    relevance_verified: bool
    relevance_score: int
    weak_match: bool
    evidence_fields: list[str] = Field(default_factory=list)


class TurnEvidence(BaseModel):
    query: str | None
    shop_carries_category: bool | None = None
    products: list[ProductEvidence] = Field(default_factory=list)
    no_match_reason: str | None = None


def build_evidence(
    *,
    query: str | None,
    raw_said: str,
    products: list[Product],
    shopping_state: dict | None,
    memory_facts: list[dict] | None,
    catalog_sample: list[Product] | None,
    platform_unavailable: bool,
) -> TurnEvidence:
    """Verify, never invent. Every product in the returned `products` list
    came from `products` (i.e. `exists=True` for all of them, by
    construction - this function never adds an id that wasn't already
    fetched); a product failing verification is dropped from the response
    entirely rather than shown with a caveat, matching the "No-Match
    Behavior" contract (never silently substitute, never present a
    non-match as a match)."""
    if platform_unavailable:
        return TurnEvidence(
            query=query, products=[], no_match_reason="platform_unavailable"
        )

    if not products:
        shop_carries_category = (
            shop_carries(query, catalog_sample or []) if query else None
        )
        return TurnEvidence(
            query=query,
            shop_carries_category=shop_carries_category,
            products=[],
            no_match_reason="no_candidates",
        )

    # Hard-filter + soft-rank via the EXISTING deterministic ranking layer -
    # same function, same semantics V1 already relies on for
    # RECOMMEND_PRODUCTS. `rank_and_filter` takes plain dicts; Product ->
    # dict via each product's own known fields, matching what that module
    # already expects from adapter results elsewhere in this codebase.
    product_dicts = [_as_dict(p) for p in products]
    shortlist, fit_reasons = mission_ranking.rank_and_filter(
        product_dicts, shopping_state, memory_facts=memory_facts
    )
    shortlisted_ids = {p["product_id"] for p in shortlist}

    top_score, best = (0, [])
    if query:
        top_score, best = best_matches_with_score(products, query)
    best_ids = {p.product_id for p in best} if query else {p.product_id for p in products}

    diverges = False
    if query:
        diverges = query_diverges_from_said(query, raw_said, candidates=products)
    weak = weak_single_word_match(query, top_score) if query else False

    evidence_products: list[ProductEvidence] = []
    for p in products:
        hard_passed = p.product_id in shortlisted_ids
        failed: list[str] = []
        if not hard_passed:
            failed.append("hard_constraint_or_out_of_stock")

        # Mirrors the actual gate `engine/execution/service.py` applies
        # (search for `weak_single_word_match(...) or
        # query_diverges_from_said(...)` there): EITHER signal alone is
        # enough to withhold confident relevance, not only both together -
        # a product sharing exactly one word (weak) or reached via a
        # query the shopper never actually said (diverges) is not a
        # confirmed match, whichever single reason applies.
        relevant = p.product_id in best_ids and not (diverges or weak)
        fields: list[str] = []
        if p.product_id in fit_reasons:
            fields.extend(fit_reasons[p.product_id])
        if p.product_id in best_ids:
            fields.append("title_or_description_overlap")

        evidence_products.append(
            ProductEvidence(
                product_id=p.product_id,
                exists=True,
                category_match=hard_passed if shopping_state else None,
                hard_constraints_passed=hard_passed,
                failed_constraints=failed,
                relevance_verified=hard_passed and relevant,
                relevance_score=top_score,
                weak_match=weak,
                evidence_fields=fields,
            )
        )

    shop_carries_category = None
    no_match_reason = None
    if not any(e.relevance_verified for e in evidence_products):
        shop_carries_category = shop_carries(query, catalog_sample or []) if query else None
        no_match_reason = "no_relevant_match"

    return TurnEvidence(
        query=query,
        shop_carries_category=shop_carries_category,
        products=evidence_products,
        no_match_reason=no_match_reason,
    )


def _as_dict(p: Product) -> dict:
    """The plain-dict shape `mission_ranking.rank_and_filter` already
    expects (see `engine/api/chat.py`'s own RECOMMEND_PRODUCTS branch,
    which builds an equivalent dict before calling it) - field names match
    that existing contract exactly, not reinvented here."""
    return {
        "product_id": p.product_id,
        "title": p.title,
        "description": p.description,
        "categories": list(p.categories or []),
        "price": str(p.price) if p.price is not None else None,
        "availability": str(p.availability),
        "rating": p.rating,
        "rating_count": p.rating_count,
    }
