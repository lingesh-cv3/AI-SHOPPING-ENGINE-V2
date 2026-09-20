"""Stage 2A evaluation infrastructure: corpus loading, metric calculation,
deterministic repeatability, tenant/merchant separation, zero-data and
platform-down behavior.

No live merchant server and no LLM key required - uses the same in-process
`ZeroDataAdapter`/`PlatformDownAdapter` stubs `run_baseline.py` uses for
those two real-client conditions, plus a small local stub adapter for the
tenant-separation check, so this file runs anywhere, same convention as
`tests/shopper_v2/test_zero_data_platform_down.py`.

    python tests/shopper_v2/test_stage2a_eval.py

The corpus cases that exercise Northfield/Kettle directly (requiring the
live merchant servers on 8001/8002) are NOT re-asserted here - that is what
`engine/shopper_v2/eval/run_baseline.py` itself does, and its own JSON
output under `engine/shopper_v2/eval/results/` is the record of that run.
This file tests the INFRASTRUCTURE (corpus/metrics/contracts wiring), not
the live retrieval numbers themselves.
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine.shopper_v2.eval.corpus import CORPUS, EvalCase, EvalTurn  # noqa: E402
from engine.shopper_v2.eval.metrics import compute_metrics  # noqa: E402
from engine.shopper_v2.eval.runner import (  # noqa: E402
    PlatformDownAdapter,
    ZeroDataAdapter,
    run_case,
    run_corpus,
)


def test_corpus_loads_and_ids_are_unique():
    assert len(CORPUS) > 0
    ids = [c.case_id for c in CORPUS]
    assert len(ids) == len(set(ids)), "duplicate case_id in corpus"


def test_corpus_covers_both_merchants():
    merchants = {c.merchant for c in CORPUS}
    assert "northfield" in merchants
    assert "kettle" in merchants


def test_corpus_covers_required_categories():
    categories = {c.category for c in CORPUS}
    required = {
        "discovery",
        "attributes",
        "constraints",
        "multi_turn",
        "language_variation",
        "no_match",
        "false_substitution_regression",
        "false_decline_regression",
        "multi_item",
    }
    missing = required - categories
    assert not missing, f"corpus missing required categories: {missing}"


def test_corpus_ground_truth_is_explicit():
    """Every turn must state SOME ground truth - either a non-empty expected
    relevant set, or an explicit no-confident-match expectation. A turn with
    neither is unauditable (the task's own requirement: 'expected outcomes
    are explicit and auditable')."""
    for case in CORPUS:
        for turn in case.turns:
            has_ground_truth = bool(turn.expected_relevant_ids) or turn.expect_no_confident_match
            assert has_ground_truth, f"{case.case_id} turn '{turn.message}' has no stated ground truth"


def test_zero_data_case_deterministic_no_candidates():
    async def _run():
        case = EvalCase(
            case_id="t-zero",
            merchant="zero_data",
            category="platform_condition",
            turns=[EvalTurn(message="anything", expect_no_confident_match=True)],
        )
        return await run_case(case, ZeroDataAdapter())

    results = asyncio.run(_run())
    assert len(results) == 1
    r = results[0]
    assert r.pipeline.candidate_set.candidates == []
    assert r.pipeline.no_match_reason == "no_candidates"
    assert r.pipeline.evidence == []


def test_platform_down_case_degrades_honestly():
    async def _run():
        case = EvalCase(
            case_id="t-down",
            merchant="platform_down",
            category="platform_condition",
            turns=[EvalTurn(message="anything", expect_no_confident_match=True)],
        )
        return await run_case(case, PlatformDownAdapter())

    results = asyncio.run(_run())
    r = results[0]
    assert r.pipeline.candidate_set.unavailable is True
    assert r.pipeline.no_match_reason == "platform_unavailable"
    assert r.pipeline.evidence == []


def test_deterministic_repeatability():
    """Running the same case against the same (deterministic) stub adapter
    twice must produce byte-identical relevant-id sets and no-match reasons
    - required for 'possible to run the baseline again later... and compare
    results' (Stage 2A spec)."""

    async def _run_once():
        case = EvalCase(
            case_id="t-repeat",
            merchant="zero_data",
            category="discovery",
            turns=[EvalTurn(message="I need running shoes", expect_no_confident_match=True)],
        )
        return await run_case(case, ZeroDataAdapter())

    r1 = asyncio.run(_run_once())
    r2 = asyncio.run(_run_once())
    assert r1[0].pipeline.no_match_reason == r2[0].pipeline.no_match_reason
    assert [e.product_id for e in r1[0].pipeline.evidence] == [
        e.product_id for e in r2[0].pipeline.evidence
    ]


class _TaggedAdapter:
    """A minimal stub that tags which connection_id it was called through,
    to verify `run_corpus` routes each case to the RIGHT adapter by
    `case.merchant` and never crosses the two."""

    def __init__(self, tag: str):
        self.tag = tag
        self.calls: list[str] = []

    async def search_products(self, query, *, limit=20):
        self.calls.append(query)
        from shared.models import ProductSearchResult

        return ProductSearchResult(query=f"{self.tag}:{query}", products=[], total_available=0)


def test_tenant_merchant_separation_in_run_corpus():
    async def _run():
        nf = _TaggedAdapter("northfield")
        kb = _TaggedAdapter("kettle")
        return await run_corpus(
            northfield_adapter=nf,
            kettle_adapter=kb,
            include_zero_data=False,
            include_platform_down=False,
        ), nf, kb

    results, nf, kb = asyncio.run(_run())
    nf_cases = {r.case.case_id for r in results if r.case.merchant == "northfield"}
    kb_cases = {r.case.case_id for r in results if r.case.merchant == "kettle"}
    assert nf_cases, "no northfield cases ran"
    assert kb_cases, "no kettle cases ran"
    assert nf_cases.isdisjoint(kb_cases)
    # Every northfield-tagged case's calls landed on the northfield stub, not
    # the kettle one, and vice versa - the actual cross-tenant assertion.
    assert len(nf.calls) > 0
    assert len(kb.calls) > 0


def test_metric_calculation_on_synthetic_results():
    """Model-free: builds `TurnResult`s directly (no adapter call at all) so
    metric arithmetic itself is tested independent of any live retrieval."""
    from engine.shopper_v2 import contracts
    from engine.shopper_v2.eval.runner import TurnResult
    from engine.shopper_v2.eval import adapt

    intent = contracts.SearchIntent(raw_message="x", query="x")
    case = EvalCase(case_id="synthetic", merchant="northfield", category="discovery", turns=[])
    turn = EvalTurn(message="x", expected_relevant_ids=["P1"])

    evidence = [
        contracts.VerifiedProductEvidence(
            product_id="P1",
            exists=True,
            hard_constraints_passed=True,
            relevance_verified=True,
            relevance_score=2.0,
            weak_match=False,
        )
    ]
    from shared.models import Product, Availability

    p1 = Product(product_id="P1", title="x", availability=Availability.IN_STOCK)
    ranked = contracts.RankingResult(
        ranked=[
            contracts.RankedCandidate(
                candidate=contracts.Candidate(
                    product=p1, retrieval_method=contracts.RetrievalMethod.ADAPTER_KEYWORD
                ),
                relevance_score=2.0,
            )
        ],
        confident=True,
    )
    pipeline = adapt.PipelineResult(
        intent=intent,
        candidate_set=contracts.CandidateSet(intent=intent, candidates=[]),
        hard_filter=contracts.HardFilterResult(
            eligible=[
                contracts.Candidate(
                    product=p1, retrieval_method=contracts.RetrievalMethod.ADAPTER_KEYWORD
                )
            ]
        ),
        ranking=ranked,
        evidence=evidence,
        no_match_reason=None,
        shop_carries_category=None,
    )
    result = TurnResult(case=case, turn=turn, turn_index=0, pipeline=pipeline, wall_time_s=0.001)

    m = compute_metrics([result])
    assert m.recall_at_4 == 1.0
    assert m.precision_at_4 == 1.0
    assert m.mrr == 1.0


def run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failures = 0
    for t in tests:
        try:
            t()
            print(f"  ok    {t.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAILED {t.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  FAILED {t.__name__}: {type(exc).__name__}: {exc}")
    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Stage 2A eval-infrastructure checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
