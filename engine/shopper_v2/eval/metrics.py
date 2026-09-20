"""Metric calculations over a set of `TurnResult`s (see `runner.py`).

Only computes a metric when the corpus actually supports it. Per the Stage
2A spec's own instruction ("do not manufacture metrics"), several requested
metrics are explicitly NOT computed here, with the reason stated in
`UNAVAILABLE_METRICS` rather than silently omitted - see
`SHOPPER_V2_BASELINE_REPORT.md` for the same list with fuller explanation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .runner import TurnResult

#: Metrics the task asked for that this module deliberately does not
#: compute, and why. Surfaced in the report rather than silently dropped.
UNAVAILABLE_METRICS: dict[str, str] = {
    "intent_extraction_accuracy": (
        "Stage 2A's baseline runner builds SearchIntent directly from each "
        "corpus case's own stated ground-truth fields, bypassing the LLM "
        "`understand()` step entirely (see runner.py's docstring) - this "
        "keeps the retrieval-pipeline baseline reproducible and free of "
        "model-call variance/cost, but means there is no LLM-produced "
        "intent to score against ground truth in this run. Measuring this "
        "honestly requires a SEPARATE, smaller, LLM-driven pass, not yet "
        "built, and would consume real budget against the documented "
        "~4-turns/minute Groq throttle."
    ),
    "token_usage_cost": (
        "The retrieval/evidence pipeline this baseline measures "
        "(retrieval.py + evidence.py) makes zero LLM calls - confirmed by "
        "reading evidence.py's imports, which call only "
        "engine.execution.relevance/mission_ranking, never "
        "ReasoningService.judge_relevance/judge_best_match. There is "
        "no token usage to report because no model call happens in the "
        "measured path."
    ),
    "reranking_latency": (
        "No reranking step exists anywhere in the current pipeline (Stage "
        "2A does not add one, per its own scope) - there is nothing to "
        "time. `ranking_s` in each TurnResult's timings IS measured and "
        "reported; it is the existing deterministic lexical scoring step, "
        "not a reranker."
    ),
}


@dataclass
class Metrics:
    n_turns: int = 0
    n_turns_with_ground_truth: int = 0
    recall_at_4: float | None = None
    precision_at_4: float | None = None
    mrr: float | None = None
    ndcg_at_4: float | None = None
    hard_constraint_violation_rate: float | None = None
    false_substitution_rate: float | None = None
    false_decline_rate: float | None = None
    no_match_correctness: float | None = None
    multi_turn_constraint_retention: float | None = None
    per_category: dict = field(default_factory=dict)


def _dcg(relevances: list[int]) -> float:
    return sum(rel / math.log2(i + 2) for i, rel in enumerate(relevances))


def _ndcg_at_k(ranked_ids: list[str], relevant_ids: set[str], k: int) -> float | None:
    if not relevant_ids:
        return None
    top = ranked_ids[:k]
    gains = [1 if pid in relevant_ids else 0 for pid in top]
    ideal = sorted(gains, reverse=True)
    idcg = _dcg(ideal)
    if idcg == 0:
        return 0.0
    return _dcg(gains) / idcg


def compute_metrics(results: list[TurnResult], *, k: int = 4) -> Metrics:
    """Computes every metric the corpus can actually support. A turn with
    `expected_relevant_ids == []` and `expect_no_confident_match=False`
    (i.e. no ground truth given at all) is excluded from
    Recall/Precision/MRR/NDCG - those turns exist only to exercise no-match/
    platform-condition behavior and have no relevant-set to score ranking
    against."""
    m = Metrics(n_turns=len(results))

    recalls: list[float] = []
    precisions: list[float] = []
    reciprocal_ranks: list[float] = []
    ndcgs: list[float] = []

    false_substitutions = 0
    false_declines = 0
    no_match_cases = 0
    no_match_correct = 0
    hard_violations = 0
    hard_checked = 0
    retention_checked = 0
    retention_correct = 0

    for r in results:
        verified_ids = [
            e.product_id for e in r.pipeline.evidence if e.relevance_verified
        ]
        # Ranked order for NDCG/MRR: pipeline.ranking.ranked is already
        # ordered best-first by the SAME lexical scorer evidence.py uses.
        ranked_ids = [rc.candidate.product.product_id for rc in r.pipeline.ranking.ranked]

        if r.turn.expected_relevant_ids:
            m.n_turns_with_ground_truth += 1
            expected = set(r.turn.expected_relevant_ids)
            got = set(verified_ids)
            recalls.append(len(got & expected) / len(expected))
            precisions.append(len(got & expected) / len(got) if got else 0.0)

            rr = 0.0
            for i, pid in enumerate(ranked_ids, start=1):
                if pid in expected:
                    rr = 1.0 / i
                    break
            reciprocal_ranks.append(rr)

            ndcg = _ndcg_at_k(ranked_ids, expected, k)
            if ndcg is not None:
                ndcgs.append(ndcg)

            # A verified product NOT in the expected set, on a case that DOES
            # name a specific expected set, is the operational definition of
            # false substitution used here - the pipeline confidently showed
            # something the traced ground truth says is wrong.
            if got - expected:
                false_substitutions += 1

        if r.turn.expect_no_confident_match:
            no_match_cases += 1
            if not verified_ids:
                no_match_correct += 1
            else:
                # The pipeline verified something confidently on a case
                # ground-truth says should have no confident match - this is
                # the false-decline corpus's inverse: a false POSITIVE where
                # honesty required silence. Counted under false_declines only
                # when the case is explicitly tagged as a false-decline
                # regression; otherwise it is a plain no-match-correctness
                # miss, counted above.
                if r.case.category == "false_decline_regression":
                    pass  # expected outcome for this category IS no-match
            if r.turn.expected_no_match_reason:
                hard_checked += 1
                if r.pipeline.no_match_reason != r.turn.expected_no_match_reason:
                    hard_violations += 1

        if r.case.category == "false_decline_regression":
            # This category's ground truth is deliberately
            # "expect_no_confident_match" (the pipeline is expected, per
            # the traced code, to fail to find the real answer) - so
            # "correct per corpus" and "false decline actually occurred"
            # are the SAME observed outcome here. Both are reported, not
            # conflated: no_match_correctness counts it as "matched
            # expectation"; false_decline_rate counts it as an actual,
            # measured false decline in the running pipeline.
            if not verified_ids:
                false_declines += 1

        if r.turn_index > 0:
            retention_checked += 1
            if r.turn.budget_amount is not None:
                over_budget_survivors = [
                    e
                    for e in r.pipeline.evidence
                    if e.relevance_verified
                    and e.product_id not in {p.product.product_id for p in r.pipeline.hard_filter.eligible}
                ]
                if not over_budget_survivors:
                    retention_correct += 1
            else:
                retention_correct += 1

    m.recall_at_4 = sum(recalls) / len(recalls) if recalls else None
    m.precision_at_4 = sum(precisions) / len(precisions) if precisions else None
    m.mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else None
    m.ndcg_at_4 = sum(ndcgs) / len(ndcgs) if ndcgs else None
    m.false_substitution_rate = (
        false_substitutions / m.n_turns_with_ground_truth
        if m.n_turns_with_ground_truth
        else None
    )
    m.false_decline_rate = (
        false_declines / len([r for r in results if r.case.category == "false_decline_regression"])
        if any(r.case.category == "false_decline_regression" for r in results)
        else None
    )
    m.no_match_correctness = (
        no_match_correct / no_match_cases if no_match_cases else None
    )
    m.hard_constraint_violation_rate = (
        hard_violations / hard_checked if hard_checked else None
    )
    m.multi_turn_constraint_retention = (
        retention_correct / retention_checked if retention_checked else None
    )

    by_cat: dict[str, list[TurnResult]] = {}
    for r in results:
        by_cat.setdefault(r.case.category, []).append(r)
    m.per_category = {cat: len(rs) for cat, rs in by_cat.items()}

    return m
