"""The Stage 2A baseline runner.

Drives `engine/shopper_v2/eval/adapt.py::run_pipeline` - itself a thin
wrapper over the UNMODIFIED Stage 1 retrieval/evidence pipeline - against
`corpus.CORPUS`, and returns one `TurnResult` per turn.

**Deliberate Stage 2A design decision: `SearchIntent` is built directly
from each corpus turn's own stated ground-truth fields
(`SearchIntent.from_shopping_state`-equivalent, built inline below), NOT
from the LLM `understand()` step Stage 1's `orchestrator.py` normally
calls first.** This is stated explicitly because it is a real choice with a
real tradeoff, not an oversight:

- It makes the baseline exactly reproducible run to run (no model-call
  variance) - required for "repeatable... compare results" (Stage 2A spec
  §18/2A, and the task's own "deterministic/repeatable" requirement).
- It isolates what THIS phase is actually measuring - candidate retrieval
  through evidence verification (steps B-F of the target architecture,
  SHOPPER_V2_STAGE2_SPEC.md §5) - from intent-extraction quality, which is
  a different, LLM-dependent concern the throttled free-tier provider
  (CLAUDE.md's "roughly four model turns a minute") makes expensive and
  noisy to measure at corpus scale.
- The cost: this baseline does NOT exercise `engine/shopper_v2/understanding.py`
  or `engine/shopper_v2/orchestrator.py` at all. `intent_extraction_accuracy`
  is therefore explicitly reported as unavailable (`metrics.py`), not
  guessed at. A separate, smaller, LLM-driven smoke pass would be needed to
  measure that specific thing, and is out of Stage 2A's scope.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from shared.interfaces import StandardCommerceInterface
from shared.models import Product, ProductSearchResult

from ..contracts import SearchIntent
from . import adapt
from .corpus import CORPUS, EvalCase, EvalTurn


@dataclass
class TurnResult:
    case: EvalCase
    turn: EvalTurn
    turn_index: int
    pipeline: "adapt.PipelineResult"
    wall_time_s: float


class ZeroDataAdapter:
    """Simulates a merchant connected this morning with an empty catalogue.

    Not a new external service and not a new merchant fixture - a small,
    in-process stub implementing only the one method `retrieve_candidates`
    calls (`search_products`), used solely to exercise the "zero data" real-
    client condition (CLAUDE.md) without seeding a fifth backend process.
    Documented here rather than silently passed off as a real platform.
    """

    connection_id = "conn_zero_data_eval"

    async def search_products(
        self, query: str, *, limit: int = 20
    ) -> ProductSearchResult:
        return ProductSearchResult(query=query, products=[], total_available=0)


class PlatformDownAdapter:
    """Simulates the adapter's own `search_products` raising - the "platform
    is down" real-client condition. Same in-process-stub reasoning as
    `ZeroDataAdapter` above."""

    connection_id = "conn_platform_down_eval"

    async def search_products(self, query: str, *, limit: int = 20):
        raise ConnectionError("simulated platform-down for Stage 2A eval")


def _build_intent(turn: EvalTurn, *, carried_positive, carried_negative, carried_budget) -> SearchIntent:
    """Combines this turn's own stated fields with whatever the case's
    earlier turns already established - the same "current turn's own
    ShoppingStateV2, carried across turns" shape `state.py`/`orchestrator.py`
    already implement at runtime (§12), built directly from ground truth
    here rather than through the persisted-state machinery, per this
    module's own docstring."""
    positive = list({*carried_positive, *turn.positive_terms})
    negative = list({*carried_negative, *turn.negative_terms})
    budget = turn.budget_amount if turn.budget_amount is not None else carried_budget
    query = turn.query or turn.message
    return SearchIntent(
        raw_message=turn.message,
        query=query,
        positive_terms=positive,
        negative_terms=negative,
        budget_amount=budget,
    ), positive, negative, budget


async def run_case(case: EvalCase, adapter) -> list[TurnResult]:
    results: list[TurnResult] = []
    positive: list[str] = []
    negative: list[str] = []
    budget: str | None = None
    for i, turn in enumerate(case.turns):
        intent, positive, negative, budget = _build_intent(
            turn, carried_positive=positive, carried_negative=negative, carried_budget=budget
        )
        shopping_state = {
            "must_have": [],
            "constraints": [],
            "priorities": [],
            "exclusions": intent.negative_terms,
            "budget_amount": intent.budget_amount,
        }
        t0 = time.perf_counter()
        pipeline_result = await adapt.run_pipeline(
            adapter, intent, shopping_state=shopping_state
        )
        wall = time.perf_counter() - t0
        results.append(
            TurnResult(
                case=case, turn=turn, turn_index=i, pipeline=pipeline_result, wall_time_s=wall
            )
        )
    return results


async def run_corpus(
    *, northfield_adapter, kettle_adapter, include_zero_data=True, include_platform_down=True
) -> list[TurnResult]:
    """Runs every case in `CORPUS` plus (optionally) one synthetic
    zero-data case and one synthetic platform-down case, against the given
    real adapter instances (constructed by the caller - see
    `run_baseline.py` for how Stage 2A's own script does this, reusing the
    same adapter construction `engine/api/deps.py` uses for the two demo
    connections, without importing that module or any of engine.api)."""
    all_results: list[TurnResult] = []
    for case in CORPUS:
        adapter = northfield_adapter if case.merchant == "northfield" else kettle_adapter
        all_results.extend(await run_case(case, adapter))

    if include_zero_data:
        zero_case = EvalCase(
            case_id="zero-data-01",
            merchant="zero_data",
            category="platform_condition",
            turns=[
                EvalTurn(
                    message="I need running shoes",
                    expect_no_confident_match=True,
                    expected_no_match_reason="no_candidates",
                )
            ],
            notes="A merchant connected this morning, no catalogue at all (CLAUDE.md's zero-data real-client condition).",
        )
        all_results.extend(await run_case(zero_case, ZeroDataAdapter()))

    if include_platform_down:
        down_case = EvalCase(
            case_id="platform-down-01",
            merchant="platform_down",
            category="platform_condition",
            turns=[
                EvalTurn(
                    message="I need running shoes",
                    expect_no_confident_match=True,
                    expected_no_match_reason="platform_unavailable",
                )
            ],
            notes="The adapter raises (CLAUDE.md's platform-down real-client condition).",
        )
        all_results.extend(await run_case(down_case, PlatformDownAdapter()))

    return all_results
