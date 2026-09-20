"""Stage 2A baseline entry point.

    python -m engine.shopper_v2.eval.run_baseline

Requires the two demo merchant backends running (per CLAUDE.md's "Running
it" section: `sample_merchant.api.main:app` on 8001,
`sample_merchant_two.api.main:app` on 8002). Does NOT require the engine
(port 8000) or any LLM credential - see `runner.py`'s own docstring for why
(the baseline builds `SearchIntent` from corpus ground truth, not from a
live `understand()` call).

Writes a timestamped, machine-readable JSON results file under
`engine/shopper_v2/eval/results/` and prints a human-readable summary.
Nothing here modifies `cv3.db`, mutates any merchant catalogue, or touches
cart/checkout/payment - every call made is `search_products`, a read-only
Standard Commerce Interface method.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from adapters.kettle import KettleAdapter
from adapters.sample import SampleMerchantAdapter

from .metrics import UNAVAILABLE_METRICS, compute_metrics
from .runner import run_corpus

RESULTS_DIR = Path(__file__).parent / "results"


def _adapters():
    """Same construction `engine/api/deps.py::build_engine` uses for the two
    demo connections - imported directly from `adapters.*`, not from
    `engine.api.deps`, so this eval package never imports anything under
    `engine.api` (keeps V2/eval isolation explicit and avoids pulling in
    ReasoningService/ExecutionService/the full Engine, none of which this
    baseline needs)."""
    northfield = SampleMerchantAdapter(
        connection_id="conn_demo",
        base_url="http://127.0.0.1:8001",
        currency="INR",
        storefront_url="http://localhost:5173",
    )
    kettle = KettleAdapter(
        connection_id="conn_kettle",
        base_url="http://127.0.0.1:8002",
        storefront_url="http://localhost:5173",
        webhook_secret="demo-kettle-webhook-secret",
    )
    return northfield, kettle


def _result_to_dict(r) -> dict:
    return {
        "case_id": r.case.case_id,
        "merchant": r.case.merchant,
        "category": r.case.category,
        "turn_index": r.turn_index,
        "message": r.turn.message,
        "expected_relevant_ids": r.turn.expected_relevant_ids,
        "expect_no_confident_match": r.turn.expect_no_confident_match,
        "expected_no_match_reason": r.turn.expected_no_match_reason,
        "retrieved_candidate_ids": [
            c.product.product_id for c in r.pipeline.candidate_set.candidates
        ],
        "candidate_set_truncated": r.pipeline.candidate_set.truncated,
        "candidate_set_unavailable": r.pipeline.candidate_set.unavailable,
        "eligible_ids": [c.product.product_id for c in r.pipeline.hard_filter.eligible],
        "rejected": [
            {"product_id": rc.candidate.product.product_id, "reason": rc.reason.value}
            for rc in r.pipeline.hard_filter.rejected
        ],
        "ranked_ids": [
            rc.candidate.product.product_id for rc in r.pipeline.ranking.ranked
        ],
        "ranking_confident": r.pipeline.ranking.confident,
        "verified_relevant_ids": [
            e.product_id for e in r.pipeline.evidence if e.relevance_verified
        ],
        "no_match_reason": r.pipeline.no_match_reason,
        "shop_carries_category": r.pipeline.shop_carries_category,
        "timings_s": {
            "retrieval": r.pipeline.timings.retrieval_s,
            "hard_filter": r.pipeline.timings.hard_filter_s,
            "ranking": r.pipeline.timings.ranking_s,
            "evidence": r.pipeline.timings.evidence_s,
            "total": r.pipeline.timings.total_s,
        },
        "wall_time_s": r.wall_time_s,
    }


async def main() -> int:
    northfield, kettle = _adapters()
    try:
        results = await run_corpus(northfield_adapter=northfield, kettle_adapter=kettle)
    finally:
        for a in (northfield, kettle):
            close = getattr(a, "close", None)
            if close is not None:
                await close()

    overall = compute_metrics(results)
    by_merchant = {}
    for merchant in {r.case.merchant for r in results}:
        subset = [r for r in results if r.case.merchant == merchant]
        by_merchant[merchant] = compute_metrics(subset)

    timings = [r.pipeline.timings.total_s for r in results if r.pipeline.timings.total_s is not None]
    perf = {
        "n_measured": len(timings),
        "mean_total_s": sum(timings) / len(timings) if timings else None,
        "max_total_s": max(timings) if timings else None,
        "min_total_s": min(timings) if timings else None,
    }

    def m_dict(m):
        return {
            "n_turns": m.n_turns,
            "n_turns_with_ground_truth": m.n_turns_with_ground_truth,
            "recall_at_4": m.recall_at_4,
            "precision_at_4": m.precision_at_4,
            "mrr": m.mrr,
            "ndcg_at_4": m.ndcg_at_4,
            "hard_constraint_violation_rate": m.hard_constraint_violation_rate,
            "false_substitution_rate": m.false_substitution_rate,
            "false_decline_rate": m.false_decline_rate,
            "no_match_correctness": m.no_match_correctness,
            "multi_turn_constraint_retention": m.multi_turn_constraint_retention,
            "per_category": m.per_category,
        }

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "corpus_size_cases": len({r.case.case_id for r in results}),
        "corpus_size_turns": len(results),
        "overall_metrics": m_dict(overall),
        "by_merchant_metrics": {k: m_dict(v) for k, v in by_merchant.items()},
        "unavailable_metrics": UNAVAILABLE_METRICS,
        "performance": perf,
        "turns": [_result_to_dict(r) for r in results],
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_path = RESULTS_DIR / f"baseline_{stamp}.json"
    out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    latest_path = RESULTS_DIR / "baseline_latest.json"
    latest_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    print(f"Wrote {out_path}")
    print(f"Cases: {payload['corpus_size_cases']}  Turns: {payload['corpus_size_turns']}")
    print("Overall metrics:", json.dumps(m_dict(overall), indent=2))
    print("Performance:", json.dumps(perf, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
