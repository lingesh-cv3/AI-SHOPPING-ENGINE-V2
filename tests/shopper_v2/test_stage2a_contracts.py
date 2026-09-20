"""Stage 2A - typed retrieval contracts.

Model-free: no LLM, no live merchant server required. Run with:

    python -m pytest tests/shopper_v2/test_stage2a_contracts.py -q

or, following this repo's `tests/shopper_v2` convention of plain
`python <file>` scripts (no pytest dependency assumed), simply:

    python tests/shopper_v2/test_stage2a_contracts.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine.shopper_v2.contracts import (
    Candidate,
    CandidateSet,
    HardFilterResult,
    RankingResult,
    RejectedCandidate,
    RejectReason,
    RetrievalMethod,
    SearchIntent,
    VerifiedProductEvidence,
)
from shared.models import Availability, Money, Product


def _product(pid="P1", title="Test Shoe", price="1000") -> Product:
    return Product(
        product_id=pid,
        title=title,
        description="a shoe",
        price=Money(amount=price, currency="INR"),
        availability=Availability.IN_STOCK,
        categories=["Footwear"],
    )


def test_search_intent_defaults():
    intent = SearchIntent(raw_message="hello", query="hello")
    assert intent.categories == []
    assert intent.positive_terms == []
    assert intent.negative_terms == []
    assert intent.ambiguous is False
    assert intent.is_comparison is False


def test_search_intent_from_shopping_state_unifies_positive_terms():
    state = {
        "must_have": ["lightweight"],
        "constraints": ["under 5000"],
        "priorities": ["comfort"],
        "exclusions": ["waterproof"],
        "budget_amount": "5000",
        "budget_currency": "INR",
        "goal": "a marathon setup",
    }
    intent = SearchIntent.from_shopping_state(
        raw_message="I need shoes", query="shoes", shopping_state=state
    )
    assert set(intent.positive_terms) == {"lightweight", "under 5000", "comfort"}
    assert intent.negative_terms == ["waterproof"]
    assert intent.budget_amount == "5000"
    assert intent.mission_context == "a marathon setup"


def test_search_intent_from_empty_state_does_not_invent_fields():
    intent = SearchIntent.from_shopping_state(
        raw_message="hi", query="hi", shopping_state=None
    )
    assert intent.positive_terms == []
    assert intent.negative_terms == []
    assert intent.budget_amount is None


def test_candidate_set_truncated_flag():
    intent = SearchIntent(raw_message="x", query="x")
    cs = CandidateSet(
        intent=intent,
        candidates=[
            Candidate(product=_product(), retrieval_method=RetrievalMethod.ADAPTER_KEYWORD)
        ],
        total_available=50,
        truncated=True,
    )
    assert cs.truncated is True
    assert len(cs.candidates) == 1


def test_hard_filter_result_eligible_rejected_partition():
    intent = SearchIntent(raw_message="x", query="x")
    c1 = Candidate(product=_product("P1"), retrieval_method=RetrievalMethod.ADAPTER_KEYWORD)
    c2 = Candidate(product=_product("P2"), retrieval_method=RetrievalMethod.ADAPTER_KEYWORD)
    result = HardFilterResult(
        eligible=[c1],
        rejected=[RejectedCandidate(candidate=c2, reason=RejectReason.OUT_OF_STOCK)],
    )
    assert result.eligible[0].product.product_id == "P1"
    assert result.rejected[0].reason == RejectReason.OUT_OF_STOCK


def test_ranking_result_confidence_flag():
    result = RankingResult(ranked=[], confident=False)
    assert result.confident is False


def test_verified_product_evidence_requires_relevance_verified_field():
    evidence = VerifiedProductEvidence(
        product_id="P1",
        exists=True,
        hard_constraints_passed=True,
        relevance_verified=False,
        relevance_score=1.0,
        weak_match=True,
    )
    assert evidence.exists is True
    assert evidence.relevance_verified is False
    assert evidence.weak_match is True


def test_reject_reason_unclassified_is_a_real_enum_member():
    assert RejectReason.UNCLASSIFIED == "unclassified"


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
    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Stage 2A contract checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if run_all() else 0)
