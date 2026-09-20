"""Phase 1/2 production discovery pipeline for Shopper V2.

Wires the Stage 2A typed contracts (`contracts.py`) into the LIVE
`/api/v2/chat` path (previously they existed only inside the offline
`engine/shopper_v2/eval/` package - PROGRESS.md's Immediate Next Work item
5), and closes Stage 2A Findings 4 and 5 (Findings 2 and 3 were closed
directly in `evidence.py`, since they are fixes to how V2 calls the
existing deterministic layer, not new pipeline behavior - see that
module's own comments).

This module calls only:
  - the adapter's own `search_products` (read-only, same as `retrieval.py`)
  - `evidence.build_evidence` (unmodified call signature; the function
    itself is now Finding-2/3-fixed)
  - `ReasoningService.judge_relevance` / `judge_best_match` (existing
    PUBLIC methods V1's `engine/execution/service.py` already uses -
    reused, not forked, and called only as a bounded fallback, never on
    every turn)
  - `search_context.py` (V2's own new table, see `db_models.py`)

Never imports `engine.decision`, `engine.risk`, or any `engine.execution`
mutation path. Never modifies `engine/execution/relevance.py` or
`engine/execution/mission_ranking.py`.
"""

from __future__ import annotations

import asyncio
import logging

from engine.reasoning.service import ReasoningService
from shared.models import Product

from . import search_context
from .contracts import Candidate, CandidateSet, RetrievalMethod, SearchIntent
from .evidence import TurnEvidence, build_evidence
from .retrieval import retrieve
from .tracing import TurnTrace

logger = logging.getLogger(__name__)

_JUDGE_TIMEOUT_S = 12.0
_CATALOG_SAMPLE_LIMIT = 40


_RETRIEVAL_LIMIT = 20


async def _do_retrieve(adapter, query: str, *, limit: int = _RETRIEVAL_LIMIT) -> CandidateSet:
    """One read-only `adapter.search_products` call, reshaped into a
    `CandidateSet`. Same call `retrieval.py::retrieve` makes, made directly
    here (not by calling that function) only so `total_available` can be
    kept to compute `CandidateSet.truncated` - `retrieval.py`'s own
    docstring deliberately discards it ("Stage 1 makes no 'this is the
    whole catalog' claim"), which is correct for Stage 1's un-typed
    `RetrievalResult` but is exactly the field `CandidateSet` exists to
    carry (SHOPPER_V2_STAGE2_SPEC.md §7). Same pattern Stage 2A's own eval
    harness already uses (`eval/adapt.py::retrieve_candidates`) - this is
    that same, already-reviewed pattern now applied to the live pipeline."""
    try:
        result = await adapter.search_products(query, limit=limit)
    except Exception:  # noqa: BLE001 - degrade the same way retrieval.py does
        logger.warning("shopper_v2 pipeline: adapter search failed", exc_info=True)
        return CandidateSet(
            intent=SearchIntent(raw_message=query, query=query),
            candidates=[],
            unavailable=True,
        )
    products = list(getattr(result, "products", result) or [])
    total_available = getattr(result, "total_available", None)
    truncated = (total_available is not None and total_available > len(products)) or (
        total_available is None and len(products) >= limit
    )
    candidates = [
        Candidate(product=p, retrieval_method=RetrievalMethod.ADAPTER_KEYWORD)
        for p in products
    ]
    return CandidateSet(
        intent=SearchIntent(raw_message=query, query=query),
        candidates=candidates,
        total_available=total_available,
        unavailable=False,
        truncated=truncated,
    )


async def run_discovery_turn(
    *,
    reasoning: ReasoningService,
    adapter,
    session_id: str,
    connection_id: str,
    message: str,
    delta_query: str | None,
    shopping_state: dict | None,
    memory_facts: list[dict] | None,
    trace: TurnTrace,
) -> tuple[TurnEvidence, list[Product], SearchIntent]:
    """The full retrieval -> evidence step for one V2 discovery turn.
    Returns (evidence, all fetched products, the SearchIntent actually
    used) - `respond.py` is unchanged and still takes exactly this shape.
    """
    primary_query = (delta_query or message or "").strip() or message
    intent = SearchIntent.from_shopping_state(
        raw_message=message, query=primary_query, shopping_state=shopping_state
    )
    trace.search_intent_query = intent.query

    candidate_set = await _do_retrieve(adapter, intent.query)
    raw_said_for_evidence = message

    if candidate_set.unavailable:
        trace.candidate_count = 0
        evidence = build_evidence(
            query=intent.query,
            raw_said=raw_said_for_evidence,
            products=[],
            shopping_state=shopping_state,
            memory_facts=memory_facts,
            catalog_sample=None,
            platform_unavailable=True,
        )
        trace.no_match_reason = evidence.no_match_reason
        return evidence, [], intent

    products = [c.product for c in candidate_set.candidates]

    # --- Stage 2A Finding 4: constraint-only-turn context fallback -------
    #
    # Purely results-driven: only fires when THIS turn's own query found
    # literally nothing, and only when a prior turn's query is actually on
    # record. A shopper switching topics ("actually, do you have kayaks")
    # still searches their own new words first and correctly finds
    # nothing on its own merits - this never overrides a real result.
    if not products:
        last_query = await search_context.get_last_effective_query(
            session_id, connection_id
        )
        if last_query and last_query != intent.query:
            fallback_set = await _do_retrieve(adapter, last_query)
            if not fallback_set.unavailable and fallback_set.candidates:
                candidate_set = fallback_set
                products = [c.product for c in candidate_set.candidates]
                intent = SearchIntent(
                    raw_message=message,
                    query=last_query,
                    categories=intent.categories,
                    positive_terms=intent.positive_terms,
                    negative_terms=intent.negative_terms,
                    budget_amount=intent.budget_amount,
                    budget_currency=intent.budget_currency,
                )
                trace.search_intent_fallback_used = True
                trace.search_intent_query = intent.query
                # `query_diverges_from_said` checks the QUERY's words
                # against what the shopper said THIS turn - correctly, for
                # a model-invented proxy term. But a carried-forward query
                # is, by construction, words the shopper said on an
                # EARLIER turn, not this one - without this, every
                # fallback-using turn would wrongly look like a diverging,
                # ungrounded query. Including the earlier turn's own real
                # words (not inventing any) keeps that check honest: the
                # query remains fully grounded in real shopper words,
                # just not only this turn's.
                raw_said_for_evidence = f"{last_query} {message}"

    trace.candidate_count = len(products)
    trace.candidate_set_truncated = candidate_set.truncated

    catalog_sample: list[Product] | None = None
    evidence = build_evidence(
        query=intent.query,
        raw_said=raw_said_for_evidence,
        products=products,
        shopping_state=shopping_state,
        memory_facts=memory_facts,
        catalog_sample=catalog_sample,
        platform_unavailable=False,
    )
    trace.eligible_count = sum(1 for e in evidence.products if e.hard_constraints_passed)
    trace.no_match_reason = evidence.no_match_reason
    trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)

    # --- Stage 2A Finding 5: restore the LLM semantic-judge fallback -----
    #
    # `evidence.py` never calls `judge_relevance`/`judge_best_match` -
    # confirmed by reading its imports (PROGRESS.md, Shopper V2 §E,
    # finding 5). V1's `engine/execution/service.py` already has and uses
    # both; this restores that SAME existing capability to the V2 path,
    # rather than building a new one. Bounded: only runs when the
    # deterministic layer found no confident match at all, only one model
    # call (judge_relevance) plus, conditionally, a second
    # (judge_best_match) - never on a turn that already has a confident
    # answer, and never mutating anything.
    if (
        evidence.no_match_reason in ("no_candidates", "no_relevant_match")
        and reasoning is not None
        and getattr(reasoning, "enabled", False)
    ):
        evidence, judged_product = await _try_semantic_judge(
            reasoning=reasoning,
            adapter=adapter,
            message=message,
            evidence=evidence,
            already_fetched=products,
            trace=trace,
        )
        if judged_product is not None and judged_product.product_id not in {
            p.product_id for p in products
        }:
            products = [*products, judged_product]
        trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)

    # Persist the effective query for a LATER constraint-only turn to fall
    # back to - only when this turn actually verified something, so a
    # dead-end query never becomes tomorrow's fallback.
    if any(e.relevance_verified for e in evidence.products):
        await search_context.set_last_effective_query(
            session_id, connection_id, intent.query
        )

    return evidence, products, intent


async def _try_semantic_judge(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    evidence: TurnEvidence,
    already_fetched: list[Product],
    trace: TurnTrace,
) -> tuple[TurnEvidence, Product | None]:
    """Fetches a bounded catalog sample (read-only browse, same shape as
    V1's `_full_catalog_sample` pattern) only when actually needed, judges
    plausibility, and - only if plausible - asks which single candidate
    best fits. Never invents a product: `judge_best_match` only ever
    returns an id present in the sample it was given, verified against
    that same set before being trusted (see `ReasoningService.judge_best_match`'s
    own docstring - this module adds no additional trust here).

    Returns (possibly-updated evidence, the real `Product` object the
    judge picked - or `None` if nothing changed). The caller merges the
    returned `Product` into its own fetched-products list, since a product
    the judge picks from the wider sample may not have been part of the
    turn's original narrow-search candidates."""
    try:
        sample_result = await retrieve(adapter, "", limit=_CATALOG_SAMPLE_LIMIT)
    except Exception:  # noqa: BLE001 - degrade to the existing verdict, never raise further
        logger.warning("shopper_v2 pipeline: catalog sample fetch failed", exc_info=True)
        return evidence, None
    sample = list(sample_result.products or [])
    if not sample:
        return evidence, None

    categories = sorted({c for p in sample for c in (p.categories or [])})
    sample_titles = [
        f"{p.title} - {p.description}"[:160] if p.description else p.title
        for p in sample[:_CATALOG_SAMPLE_LIMIT]
    ]

    trace.llm_judge_used = True
    trace.model_calls += 1
    try:
        plausible = await asyncio.wait_for(
            reasoning.judge_relevance(
                need=message, categories=categories, sample_titles=sample_titles
            ),
            timeout=_JUDGE_TIMEOUT_S,
        )
    except TimeoutError:
        logger.info("shopper_v2 pipeline: semantic relevance judgment timed out")
        plausible = None
    except Exception:  # noqa: BLE001 - same degrade-honestly rule as V1's own call site
        logger.exception("shopper_v2 pipeline: semantic relevance judgment failed")
        plausible = None
    trace.llm_judge_result = plausible

    if not plausible:
        return evidence, None

    candidates_by_id = {p.product_id: p for p in sample}
    payload = [
        {"product_id": p.product_id, "title": p.title, "description": p.description}
        for p in sample[:12]
    ]
    trace.model_calls += 1
    try:
        picked_id = await asyncio.wait_for(
            reasoning.judge_best_match(need=message, candidates=payload),
            timeout=_JUDGE_TIMEOUT_S,
        )
    except TimeoutError:
        logger.info("shopper_v2 pipeline: best-match judgment timed out")
        picked_id = None
    except Exception:  # noqa: BLE001
        logger.exception("shopper_v2 pipeline: best-match judgment failed")
        picked_id = None

    if picked_id is None or picked_id not in candidates_by_id:
        return evidence, None

    picked_product = candidates_by_id[picked_id]
    existing_ids = {e.product_id for e in evidence.products}
    if picked_id not in existing_ids:
        from .evidence import ProductEvidence

        new_entry = ProductEvidence(
            product_id=picked_id,
            exists=True,
            category_match=None,
            hard_constraints_passed=True,
            relevance_verified=True,
            relevance_score=0,
            weak_match=False,
            evidence_fields=["semantic_judge_match"],
        )
        new_evidence = TurnEvidence(
            query=evidence.query,
            shop_carries_category=True,
            products=[*evidence.products, new_entry],
            no_match_reason=None,
        )
        return new_evidence, picked_product

    # Already among this turn's fetched candidates but the deterministic
    # layer withheld confidence - upgrade that one entry rather than
    # duplicate it.
    updated = [
        e.model_copy(
            update={
                "relevance_verified": True,
                "evidence_fields": [*e.evidence_fields, "semantic_judge_match"],
            }
        )
        if e.product_id == picked_id
        else e
        for e in evidence.products
    ]
    new_evidence = TurnEvidence(
        query=evidence.query,
        shop_carries_category=True,
        products=updated,
        no_match_reason=(
            None if any(e.relevance_verified for e in updated) else evidence.no_match_reason
        ),
    )
    return new_evidence, picked_product
