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

from engine.execution import mission_ranking, semantic_retrieval
from engine.reasoning.service import ReasoningService
from shared.models import Product

from . import search_context
from .contracts import Candidate, CandidateSet, RetrievalMethod, SearchIntent
from .evidence import TurnEvidence, build_evidence, product_as_dict
from .retrieval import retrieve
from .tracing import TurnTrace

logger = logging.getLogger(__name__)

_JUDGE_TIMEOUT_S = 12.0
_CATALOG_SAMPLE_LIMIT = 40
#: Batch 4: semantic retrieval's own wider sample - deliberately larger
#: than `_CATALOG_SAMPLE_LIMIT` (the LLM judge's sample), since TF-IDF
#: scoring is a cheap, deterministic, no-model-call operation and can
#: afford to look at more of the catalog per turn than a paid model call
#: would. Still an explicit, disclosed bound (the same honesty
#: `engine/execution/service.py::_MAX_CATALOG_SCAN` already applies), not
#: the whole catalog - a real 40k-SKU client is not fully covered by this
#: either, and this module makes no claim that it is.
_SEMANTIC_SAMPLE_LIMIT = 500
#: Cost-bounded retry cap for `_try_semantic_judge`'s pick-then-verify loop
#: - each attempt is one more `judge_best_match` model call, so this bounds
#: the worst case to 3 extra calls on top of the 1 `judge_relevance` call,
#: never unbounded.
_MAX_JUDGE_PICK_ATTEMPTS = 3


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
    constraint_only_turn: bool = False,
    shopping_state: dict | None,
    memory_facts: list[dict] | None,
    trace: TurnTrace,
) -> tuple[TurnEvidence, list[Product], SearchIntent]:
    """The full retrieval -> evidence step for one V2 discovery turn.
    Returns (evidence, all fetched products, the SearchIntent actually
    used) - `respond.py` is unchanged and still takes exactly this shape.

    `constraint_only_turn` (from `orchestrator.py`'s own real `StateDelta` -
    never a guess at word content) makes `SearchIntent` genuinely drive
    WHICH query is searched first, not just feed the semantic-judge
    rescue: a turn that stated a new budget with no new goal of its own
    ("under 2000" alone) searches the carried-forward topic directly,
    skipping the guaranteed-empty search Finding 4's reactive fallback
    below would otherwise have to wait for.
    """
    primary_query = (delta_query or message or "").strip() or message
    proactive_carry_forward_used = False
    if constraint_only_turn:
        last_query = await search_context.get_last_effective_query(
            session_id, connection_id
        )
        if last_query:
            primary_query = last_query
            proactive_carry_forward_used = True

    intent = SearchIntent.from_shopping_state(
        raw_message=message, query=primary_query, shopping_state=shopping_state
    )
    trace.search_intent_query = intent.query
    trace.search_intent_fallback_used = proactive_carry_forward_used

    candidate_set = await _do_retrieve(adapter, intent.query)
    # A carried-forward query is, by construction, words the shopper said
    # on an EARLIER turn, not this one - `query_diverges_from_said` (called
    # inside `build_evidence` below) checks the query's words against what
    # the shopper said THIS turn, so without this the proactive path would
    # wrongly look like a diverging, ungrounded query on every turn it
    # fires for. Same reasoning Finding 4's reactive fallback already
    # applies for the identical shape - see its own comment below.
    raw_said_for_evidence = (
        f"{primary_query} {message}" if proactive_carry_forward_used else message
    )

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

    # --- Batch 4: TF-IDF semantic retrieval widener ----------------------
    #
    # Tried BEFORE the LLM judge below, deliberately: this is a free,
    # deterministic, no-model-call re-score - cheaper and faster than a
    # model call, so it is worth trying first whenever the adapter's own
    # narrow (title-only, in this codebase's demo adapters) keyword search
    # already came back weak/empty. Never replaces authoritative catalog
    # data: every candidate it can possibly surface is a real `Product`
    # already fetched this same turn from the adapter's own wide sample,
    # and every one still goes through the EXACT SAME `build_evidence`
    # hard-filter/verification pass below as any keyword-found candidate -
    # this step only WIDENS the candidate set `build_evidence` sees, never
    # marks anything verified itself.
    if evidence.no_match_reason in ("no_candidates", "no_relevant_match"):
        try:
            wide_result = await retrieve(adapter, "", limit=_SEMANTIC_SAMPLE_LIMIT)
        except Exception:  # noqa: BLE001 - degrade to the existing verdict, never raise further
            wide_result = None
        if wide_result is not None and not wide_result.unavailable and wide_result.products:
            already_ids = {p.product_id for p in products}
            wide_by_id = {p.product_id: p for p in wide_result.products}
            wide_dicts = [product_as_dict(p) for p in wide_result.products]
            matches = semantic_retrieval.semantic_matches(
                intent.query or message, wide_dicts, exclude_ids=already_ids
            )
            trace.semantic_retrieval_tried = True
            trace.semantic_retrieval_candidates = len(matches)
            if matches:
                added = [
                    wide_by_id[m.product_id] for m in matches if m.product_id in wide_by_id
                ]
                products = [*products, *added]
                evidence = build_evidence(
                    query=intent.query,
                    raw_said=raw_said_for_evidence,
                    products=products,
                    shopping_state=shopping_state,
                    memory_facts=memory_facts,
                    catalog_sample=None,
                    platform_unavailable=False,
                )
                trace.no_match_reason = evidence.no_match_reason
                trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
                trace.semantic_retrieval_verified = sum(
                    1
                    for e in evidence.products
                    if e.relevance_verified and e.product_id in {m.product_id for m in matches}
                )

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
            shopping_state=shopping_state,
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


def _judge_pick_passes_hard_filter(product: Product, shopping_state: dict | None) -> bool:
    """The semantic judge only ever proposes a product it found plausible
    by category/description - it has no concept of the shopper's stated
    budget or exclusions, because it is never given `shopping_state` to
    reason over. Structural fix for a live-reproduced bug: a query like
    "lightweight running shoes under 2000" found no literal keyword match,
    the judge picked a real running shoe on category grounds alone (the
    Trailblazer, INR 4299), and the caller marked it `relevance_verified`
    with no budget check at all - a genuine hard-constraint violation
    reaching the shopper. Reuses the SAME deterministic hard-filter
    (`mission_ranking.rank_and_filter`) every other candidate in this turn
    was already checked against, on a singleton list - not a new
    heuristic, not a second budget-parsing implementation."""
    from .evidence import product_as_dict

    shortlist, _ = mission_ranking.rank_and_filter(
        [product_as_dict(product)], shopping_state, limit=1
    )
    return bool(shortlist)


async def _try_semantic_judge(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    evidence: TurnEvidence,
    already_fetched: list[Product],
    shopping_state: dict | None,
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
    turn's original narrow-search candidates.

    The judge's pick is advisory only, exactly like every other candidate
    this turn: it is never marked `relevance_verified` unless it ALSO
    passes the same deterministic hard filter (budget/exclusions/stock)
    every other product was checked against - see
    `_judge_pick_passes_hard_filter`. A pick that fails is treated as no
    match, not silently downgraded to unverified-but-shown."""
    # Structural fix: `already_fetched` (this turn's REAL search candidates)
    # was accepted as a parameter but never actually read here - the judge
    # was always shown a generic, query-blind catalog browse instead of
    # what this turn's own search already surfaced. Prefer the real,
    # already-fetched candidates when there are any; only fall back to a
    # blind browse when this turn's search itself found nothing at all
    # (`no_candidates`), so the judge still has something to reason over.
    if already_fetched:
        sample = list(already_fetched)
    else:
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

    # Bounded retry: if the judge's pick fails the shopper's own hard
    # constraints (budget/exclusions/stock), it does not mean nothing in
    # the sample qualifies - only that this particular pick doesn't. Retry
    # with that pick excluded, so a genuinely in-budget candidate sitting
    # in the SAME sample (e.g. a 699 INR product the judge overlooked in
    # favour of a 4299 INR one) gets a real second look, rather than the
    # whole rescue giving up after one wrong guess. Capped at
    # `_MAX_JUDGE_PICK_ATTEMPTS` - this is a cost-bounded retry, not a loop
    # that could run away on a stubborn model (see the Groq-throttle
    # constraint this package's own docs call "the single most concrete
    # constraint Stage 2 must design against").
    excluded_ids: set[str] = set()
    picked_id: str | None = None
    picked_product: Product | None = None
    for _attempt in range(_MAX_JUDGE_PICK_ATTEMPTS):
        remaining = [p for p in sample if p.product_id not in excluded_ids][:12]
        if not remaining:
            break
        payload = [
            {"product_id": p.product_id, "title": p.title, "description": p.description}
            for p in remaining
        ]
        trace.model_calls += 1
        try:
            candidate_id = await asyncio.wait_for(
                reasoning.judge_best_match(need=message, candidates=payload),
                timeout=_JUDGE_TIMEOUT_S,
            )
        except TimeoutError:
            logger.info("shopper_v2 pipeline: best-match judgment timed out")
            candidate_id = None
        except Exception:  # noqa: BLE001
            logger.exception("shopper_v2 pipeline: best-match judgment failed")
            candidate_id = None

        if candidate_id is None or candidate_id not in candidates_by_id:
            break

        candidate_product = candidates_by_id[candidate_id]
        if _judge_pick_passes_hard_filter(candidate_product, shopping_state):
            picked_id, picked_product = candidate_id, candidate_product
            trace.llm_judge_pick_passed_hard_filter = True
            break

        logger.info(
            "shopper_v2 pipeline: semantic judge picked %s but it fails the "
            "shopper's own hard constraints (budget/exclusion/stock) - "
            "excluding it and giving the judge another look",
            candidate_id,
        )
        trace.llm_judge_pick_passed_hard_filter = False
        excluded_ids.add(candidate_id)

    if picked_id is None or picked_product is None:
        return evidence, None

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
    # duplicate it. `hard_constraints_passed` is already re-verified above
    # via `_judge_pick_passes_hard_filter` (the same deterministic filter,
    # not this entry's possibly-stale flag), so it is safe to also set it
    # True here rather than leave a verified-but-marked-failed contradiction.
    updated = [
        e.model_copy(
            update={
                "hard_constraints_passed": True,
                "failed_constraints": [],
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
