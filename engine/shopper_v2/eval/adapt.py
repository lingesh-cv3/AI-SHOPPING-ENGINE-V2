"""Runs Stage 1's EXISTING retrieval/evidence pipeline for one turn and
reshapes the result into Stage 2A's typed contracts (`contracts.py`).

Every product-relevance decision below is made by calling the SAME
functions Stage 1's `retrieval.py`/`evidence.py` already call
(`adapter.search_products`, `engine.execution.mission_ranking.rank_and_filter`,
`engine.execution.relevance.best_matches_with_score`/`weak_single_word_match`/
`query_diverges_from_said`) - nothing here re-derives relevance from
scratch. Where a typed field needs information those functions don't
surface directly (e.g. `CandidateSet.truncated`, per-candidate rejection
`reason`), this module computes it from PUBLIC data only (the real `Product`
fields, `total_available`, and the calling `SearchIntent`) rather than
reaching into any module's private helpers - see `contracts.py`'s own note
on `RejectReason.UNCLASSIFIED` for the one place this has a real, honestly
documented gap.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from engine.execution import mission_ranking
from engine.execution.relevance import (
    best_matches_with_score,
    query_diverges_from_said,
    weak_single_word_match,
)
from engine.shopper_v2.evidence import build_evidence
from shared.models import Availability, Product

from ..contracts import (
    Candidate,
    CandidateSet,
    HardFilterResult,
    RankedCandidate,
    RankingResult,
    RejectedCandidate,
    RejectReason,
    RetrievalMethod,
    SearchIntent,
    VerifiedProductEvidence,
)

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20


@dataclass
class Timings:
    """Wall-clock time for each pipeline stage, seconds. `None` when a stage
    did not run (e.g. platform unavailable short-circuits everything after
    retrieval)."""

    retrieval_s: float | None = None
    hard_filter_s: float | None = None
    ranking_s: float | None = None
    evidence_s: float | None = None
    total_s: float | None = None


@dataclass
class PipelineResult:
    intent: SearchIntent
    candidate_set: CandidateSet
    hard_filter: HardFilterResult
    ranking: RankingResult
    evidence: list[VerifiedProductEvidence]
    no_match_reason: str | None
    shop_carries_category: bool | None
    timings: Timings = field(default_factory=Timings)


async def retrieve_candidates(
    adapter, intent: SearchIntent, *, limit: int = _DEFAULT_LIMIT
) -> tuple[CandidateSet, bool]:
    """The same call Stage 1's `retrieval.retrieve` makes
    (`adapter.search_products`), kept separate here only so `total_available`
    (discarded by `retrieval.retrieve` - see that module's own docstring on
    why Stage 1 makes no "whole catalog" claim) can be captured for
    `CandidateSet.truncated`, Stage 2A's own measurement need. Returns
    (CandidateSet, unavailable)."""
    try:
        result = await adapter.search_products(intent.query, limit=limit)
    except Exception:  # noqa: BLE001 - same degrade-honestly rule as retrieval.py
        logger.warning("eval retrieval: adapter search failed", exc_info=True)
        return (
            CandidateSet(intent=intent, candidates=[], unavailable=True),
            True,
        )
    products: list[Product] = list(getattr(result, "products", result) or [])
    total_available = getattr(result, "total_available", None)
    truncated = (
        total_available is not None and total_available > len(products)
    ) or (total_available is None and len(products) >= limit)
    candidates = [
        Candidate(product=p, retrieval_method=RetrievalMethod.ADAPTER_KEYWORD)
        for p in products
    ]
    return (
        CandidateSet(
            intent=intent,
            candidates=candidates,
            total_available=total_available,
            unavailable=False,
            truncated=truncated,
        ),
        False,
    )


def _price_amount(p: Product) -> Decimal | None:
    if p.price is None:
        return None
    try:
        return Decimal(str(p.price.amount))
    except (InvalidOperation, AttributeError, TypeError):
        return None


def hard_filter(candidate_set: CandidateSet, intent: SearchIntent) -> HardFilterResult:
    """Splits `candidate_set` into eligible/rejected using
    `mission_ranking.rank_and_filter` itself as the authority - called once
    with `limit` set to the full candidate count so its hard-filter pass
    (exclusions/budget/stock) is captured WITHOUT its separate top-N
    soft-ranking cut also being applied here. This is the one place Stage 2A
    found the spec's idealized "hard filter, then rank" separation does not
    exist as two functions in the actual repository - `rank_and_filter` does
    both in one pass (see SHOPPER_V2_BASELINE_REPORT.md, "discrepancies from
    the spec"). Calling it with an unbounded limit is how this module
    recovers the hard-filter-only view without touching or duplicating that
    function's logic.

    Rejection `reason` is then classified from PUBLIC fields only
    (`Product.availability`, price vs. `intent.budget_amount`) - see
    `RejectReason.UNCLASSIFIED`'s own docstring in `contracts.py` for what
    this cannot confidently attribute.
    """
    budget = None
    if intent.budget_amount:
        try:
            budget = Decimal(intent.budget_amount)
        except InvalidOperation:
            budget = None

    products = [c.product for c in candidate_set.candidates]
    by_id = {p.product_id: c for c, p in zip(candidate_set.candidates, products)}

    shopping_state = {
        "must_have": [],
        "constraints": [],
        "priorities": [],
        "exclusions": intent.negative_terms,
        "budget_amount": intent.budget_amount,
    }
    product_dicts = [_as_dict(p) for p in products]
    survivors, _ = mission_ranking.rank_and_filter(
        product_dicts, shopping_state, limit=max(len(product_dicts), 1)
    )
    survivor_ids = {p["product_id"] for p in survivors}

    eligible: list[Candidate] = []
    rejected: list[RejectedCandidate] = []
    for c in candidate_set.candidates:
        p = c.product
        if p.product_id in survivor_ids:
            eligible.append(c)
            continue
        if p.availability == Availability.OUT_OF_STOCK:
            reason = RejectReason.OUT_OF_STOCK
        elif budget is not None and _price_amount(p) is not None and _price_amount(p) > budget:
            reason = RejectReason.OVER_BUDGET
        elif intent.negative_terms:
            reason = RejectReason.EXCLUDED_BY_SHOPPER
        else:
            reason = RejectReason.UNCLASSIFIED
        rejected.append(RejectedCandidate(candidate=c, reason=reason))

    return HardFilterResult(eligible=eligible, rejected=rejected)


def rank(
    eligible: list[Candidate], intent: SearchIntent, raw_said: str
) -> RankingResult:
    """Orders `eligible` using the SAME deterministic lexical scorer Stage 1's
    `evidence.py` already uses (`best_matches_with_score`), called once per
    candidate to get an individual score (that function's own public
    contract returns only the winning tier as a group, not a per-item
    score - calling it with a one-item list per candidate reuses the exact
    same `_overlap`/`_significant_words` logic without reaching into its
    private helpers directly)."""
    products = [c.product for c in eligible]
    ranked: list[RankedCandidate] = []
    for c in eligible:
        score, _ = best_matches_with_score([c.product], intent.query) if intent.query else (0, [])
        ranked.append(
            RankedCandidate(
                candidate=c,
                relevance_score=float(score),
                scoring_method="lexical_overlap",
            )
        )
    ranked.sort(key=lambda r: r.relevance_score, reverse=True)

    top_score = ranked[0].relevance_score if ranked else 0
    weak = weak_single_word_match(intent.query, int(top_score)) if intent.query else False
    diverges = (
        query_diverges_from_said(intent.query, raw_said, candidates=products)
        if intent.query
        else False
    )
    for r in ranked:
        r.weak_match = weak
        r.diverges_from_said = diverges

    confident = any(
        r.relevance_score == top_score and top_score > 0 for r in ranked
    ) and not (weak or diverges)

    return RankingResult(ranked=ranked, confident=confident)


def _as_dict(p: Product) -> dict:
    """Identical shape to `evidence.py::_as_dict` - kept as its own copy
    (not imported) because it's a private helper in that module; this is a
    plain reshape of public `Product` fields, not business logic, so
    duplicating the ~10-line dict literal here does not duplicate any
    decision-making."""
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


async def run_pipeline(
    adapter,
    intent: SearchIntent,
    *,
    shopping_state: dict | None,
    catalog_sample: list[Product] | None = None,
    limit: int = _DEFAULT_LIMIT,
) -> PipelineResult:
    """One eval turn, start to finish, timed at each stage. Reuses
    `evidence.build_evidence` (Stage 1's real, unmodified function) for the
    evidence step rather than re-deriving verification logic - see this
    module's own docstring."""
    t0 = time.perf_counter()

    t_retr0 = time.perf_counter()
    candidate_set, unavailable = await retrieve_candidates(adapter, intent, limit=limit)
    t_retr1 = time.perf_counter()

    if unavailable:
        total = time.perf_counter() - t0
        return PipelineResult(
            intent=intent,
            candidate_set=candidate_set,
            hard_filter=HardFilterResult(),
            ranking=RankingResult(),
            evidence=[],
            no_match_reason="platform_unavailable",
            shop_carries_category=None,
            timings=Timings(retrieval_s=t_retr1 - t_retr0, total_s=total),
        )

    t_hf0 = time.perf_counter()
    hf = hard_filter(candidate_set, intent)
    t_hf1 = time.perf_counter()

    t_rank0 = time.perf_counter()
    ranking = rank(hf.eligible, intent, intent.raw_message)
    t_rank1 = time.perf_counter()

    t_ev0 = time.perf_counter()
    products = [c.product for c in candidate_set.candidates]
    turn_evidence = build_evidence(
        query=intent.query,
        raw_said=intent.raw_message,
        products=products,
        shopping_state=shopping_state,
        memory_facts=None,
        catalog_sample=catalog_sample,
        platform_unavailable=False,
    )
    t_ev1 = time.perf_counter()

    verified = [
        VerifiedProductEvidence(
            product_id=pe.product_id,
            exists=pe.exists,
            category_match=pe.category_match,
            hard_constraints_passed=pe.hard_constraints_passed,
            failed_constraints=list(pe.failed_constraints),
            relevance_verified=pe.relevance_verified,
            relevance_score=float(pe.relevance_score),
            scoring_method="lexical_overlap",
            weak_match=pe.weak_match,
            diverges_from_said=ranking.confident is False
            and any(r.diverges_from_said for r in ranking.ranked),
            truncated_candidate_set=candidate_set.truncated,
            evidence_fields=list(pe.evidence_fields),
        )
        for pe in turn_evidence.products
    ]

    total = time.perf_counter() - t0
    return PipelineResult(
        intent=intent,
        candidate_set=candidate_set,
        hard_filter=hf,
        ranking=ranking,
        evidence=verified,
        no_match_reason=turn_evidence.no_match_reason,
        shop_carries_category=turn_evidence.shop_carries_category,
        timings=Timings(
            retrieval_s=t_retr1 - t_retr0,
            hard_filter_s=t_hf1 - t_hf0,
            ranking_s=t_rank1 - t_rank0,
            evidence_s=t_ev1 - t_ev0,
            total_s=total,
        ),
    )
