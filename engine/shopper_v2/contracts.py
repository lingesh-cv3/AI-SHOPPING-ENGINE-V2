"""Stage 2A - typed retrieval contracts.

Per SHOPPER_V2_STAGE2_SPEC.md sections 6-10. These types give the existing
Stage 1 retrieval/evidence pipeline (`retrieval.py`, `evidence.py`) explicit,
independently testable boundaries at each step - they do NOT introduce a new
retrieval algorithm, a new adapter method, or any new scoring heuristic.
Every field here is either already-real data the pipeline already produces
(reshaped into a typed home) or a new field whose only job is to make an
already-computed fact (e.g. "was this candidate set truncated by a page
limit") inspectable and measurable, per Stage 2A's own objective.

Ownership, stated once here rather than per-field:

- `SearchIntent` may be produced by the model (Understanding) OR built
  directly from a known/expected state (the Stage 2A evaluation corpus does
  the latter, deliberately, to keep the baseline reproducible - see
  `engine/shopper_v2/eval/runner.py`'s own docstring).
- Every step after `SearchIntent` (candidate retrieval, hard filtering,
  ranking, evidence) is deterministic Python over real adapter data. No
  field in `CandidateSet`, `HardFilterResult`, `RankingResult`, or
  `VerifiedProductEvidence` is ever written from an LLM's own claim about
  price, stock, existence, or category - those always come from the real
  `Product` object the adapter returned, or from the existing deterministic
  helpers in `engine.execution.relevance` / `engine.execution.mission_ranking`.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from shared.models import Product


class SearchIntent(BaseModel):
    """What the shopper means to search for, this turn - kept deliberately
    separate from `ProposedAction`/`ActionType` (per the Stage 2 spec's own
    instruction not to reuse action semantics for search intent, even though
    `ProposedAction.parameters` can carry a similar-looking query string).

    Every field mirrors data this codebase already has somewhere - either on
    `StateDelta`/`ShoppingStateV2` (`engine/shopper_v2/state.py`), or as the
    raw shopper message - reshaped into one typed object rather than
    threaded through two different call sites. No field here is invented
    beyond what Stage 1's existing understanding step can actually produce.
    """

    raw_message: str
    query: str
    categories: list[str] = Field(default_factory=list)
    positive_terms: list[str] = Field(default_factory=list)
    negative_terms: list[str] = Field(default_factory=list)
    budget_amount: str | None = None
    budget_currency: str | None = None
    quantity: int | None = None
    mission_context: str | None = None
    is_comparison: bool = False
    ambiguous: bool = False

    @classmethod
    def from_shopping_state(
        cls, *, raw_message: str, query: str, shopping_state: dict | None
    ) -> "SearchIntent":
        """Build a `SearchIntent` from the same `ShoppingStateV2.as_prompt_dict()`
        shape `evidence.py`/`mission_ranking.py` already consume - the normal
        runtime path (see `orchestrator.py`). `positive_terms` unifies
        must_have/constraints/priorities exactly the way
        `mission_ranking.rank_and_filter` already treats them as one
        additive soft-signal set (see that module's own docstring)."""
        state = shopping_state or {}
        positive = [
            *(state.get("must_have") or []),
            *(state.get("constraints") or []),
            *(state.get("priorities") or []),
        ]
        return cls(
            raw_message=raw_message,
            query=query,
            positive_terms=[t for t in positive if t],
            negative_terms=[e for e in (state.get("exclusions") or []) if e],
            budget_amount=(
                str(state["budget_amount"]) if state.get("budget_amount") else None
            ),
            budget_currency=state.get("budget_currency"),
            mission_context=state.get("goal"),
        )


class RetrievalMethod(str, Enum):
    """The only method Stage 2A's baseline exercises is `ADAPTER_KEYWORD` -
    the existing `adapter.search_products` call. No other method is
    implemented in Stage 2A; the remaining members exist only so later
    stages (2C+) do not need a breaking enum change, per the spec's §7."""

    ADAPTER_KEYWORD = "adapter_keyword"
    LEXICAL_INDEX = "lexical_index"
    SEMANTIC_INDEX = "semantic_index"


class Candidate(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    product: Product
    retrieval_method: RetrievalMethod
    retrieval_score: float | None = None


class CandidateSet(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    intent: SearchIntent
    candidates: list[Candidate] = Field(default_factory=list)
    total_available: int | None = None
    unavailable: bool = False
    #: True whenever the returned candidate list could plausibly be less
    #: than a full catalog scan would find - i.e. the adapter's own
    #: `limit` was reached, or `total_available` is unknown. This is the
    #: field CLAUDE.md's `_catalog_alerts`/`search_products("", limit=100)`
    #: worked example exists to guard against, generalized to retrieval -
    #: see SHOPPER_V2_STAGE2_SPEC.md §7.
    truncated: bool = False


class RejectReason(str, Enum):
    EXCLUDED_BY_SHOPPER = "excluded_by_shopper"
    OUT_OF_STOCK = "out_of_stock"
    OVER_BUDGET = "over_budget"
    AMBIGUOUS_EXCLUSION_MATCH = "ambiguous_exclusion_match"
    #: Stage 2A addition, beyond the spec's original four reasons (§8):
    #: `mission_ranking.rank_and_filter`'s hard-filter pass (exclusion
    #: matching via its private `_matches`/`_uncertain` helpers) is not
    #: exposed publicly, so the eval classifier
    #: (`engine/shopper_v2/eval/adapt.py::classify_rejection`) can only
    #: confidently attribute a rejection to OUT_OF_STOCK or OVER_BUDGET from
    #: public `Product`/`SearchIntent` fields. A candidate that failed
    #: `rank_and_filter`'s full-length pass for any other reason (almost
    #: always a real exclusion match this classifier cannot independently
    #: re-derive without duplicating private logic) is labeled UNCLASSIFIED
    #: rather than guessed at - see SHOPPER_V2_BASELINE_REPORT.md's
    #: "discrepancies from the spec" section.
    UNCLASSIFIED = "unclassified"


class RejectedCandidate(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    candidate: Candidate
    reason: RejectReason


class HardFilterResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    eligible: list[Candidate] = Field(default_factory=list)
    rejected: list[RejectedCandidate] = Field(default_factory=list)


class RankedCandidate(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    candidate: Candidate
    relevance_score: float
    scoring_method: Literal["lexical_overlap", "llm_judge", "combined"] = (
        "lexical_overlap"
    )
    weak_match: bool = False
    diverges_from_said: bool = False


class RankingResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    ranked: list[RankedCandidate] = Field(default_factory=list)
    #: True only when at least one ranked candidate is neither a weak match
    #: nor diverges from what the shopper actually said - mirrors
    #: `evidence.py`'s existing `relevant = in best_ids and not (diverges
    #: or weak)` gate, named explicitly here rather than left inline.
    confident: bool = False


NoMatchReason = Literal[
    "no_candidates",
    "all_failed_hard_constraints",
    "no_relevant_match",
    "category_present_product_absent",
    "platform_unavailable",
    None,
]


class VerifiedProductEvidence(BaseModel):
    """Supersedes `evidence.ProductEvidence` for Stage 2A's typed-contract
    purposes. Deliberately NOT a replacement of `evidence.py` itself in
    Stage 2A - see `engine/shopper_v2/eval/adapt.py` for the one, narrow
    place that builds one of these FROM an existing `ProductEvidence`,
    rather than duplicating `evidence.build_evidence`'s logic. Stage 2A does
    not change what `evidence.py` computes, only gives its result a fuller
    typed shape for measurement."""

    product_id: str
    exists: bool
    category_match: bool | None = None
    hard_constraints_passed: bool
    failed_constraints: list[str] = Field(default_factory=list)
    relevance_verified: bool
    relevance_score: float
    scoring_method: str = "lexical_overlap"
    weak_match: bool
    diverges_from_said: bool = False
    truncated_candidate_set: bool = False
    evidence_fields: list[str] = Field(default_factory=list)
