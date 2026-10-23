"""Structured per-turn tracing for Shopper V2.

One `TurnTrace` per `/api/v2/chat` call, capturing every stage named in the
implementation brief: turn/session/connection ids, the selected capability,
understanding result, state delta/version, retrieval query, candidate
count, filtering/ranking/evidence results, model-call count, and per-stage
latency. Logged as one structured line (`logger.info` with `extra=`) rather
than adopting OpenTelemetry/LangSmith - no measured need for a distributed
tracing backend exists yet (single process, two demo merchants), and
CLAUDE.md's own rule is to add infrastructure only when evidence justifies
it. `TurnTrace.as_dict()` is also returned inline on `ChatReplyV2` (see
`engine/api/chat_v2.py`) so a failure's stage is visible without grepping
logs - "make it possible to identify where a failure occurred" without a
new external dependency.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field

logger = logging.getLogger("shopper_v2.trace")


@dataclass
class TurnTrace:
    turn_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    connection_id: str = ""

    capability: str | None = None
    understanding_used_model: bool | None = None
    understanding_model_name: str | None = None
    state_version: int | None = None

    search_intent_query: str | None = None
    search_intent_fallback_used: bool = False
    candidate_count: int | None = None
    candidate_set_truncated: bool | None = None
    eligible_count: int | None = None
    ranked_count: int | None = None
    ranking_confident: bool | None = None
    llm_judge_used: bool = False
    llm_judge_result: bool | None = None
    #: None when the judge never picked a candidate this turn; True/False
    #: when it did, recording whether that pick also passed the shopper's
    #: own hard constraints (budget/exclusions/stock) before being trusted
    #: - see `pipeline.py::_judge_pick_passes_hard_filter`.
    llm_judge_pick_passed_hard_filter: bool | None = None
    verified_count: int | None = None
    no_match_reason: str | None = None

    #: Batch 4 (V2 completion audit): whether the TF-IDF semantic-retrieval
    #: widener (engine/execution/semantic_retrieval.py) ran this turn - only
    #: ever True when the adapter's own keyword search already came back
    #: weak/empty - how many products it surfaced above its similarity
    #: floor, and how many of those actually passed hard-filter/relevance
    #: verification afterward (never trusted on its own say-so).
    semantic_retrieval_tried: bool = False
    semantic_retrieval_candidates: int | None = None
    semantic_retrieval_verified: int | None = None

    #: Phase 4 continuation - whether the general cart-intent interpreter
    #: (`intent_resolution.py`) ran this turn, and what it found. Absent
    #: (both None/False) means it never ran at all (no decision/gate/
    #: execution wired, or nothing currently shown to refer to).
    cart_intent_checked: bool = False
    cart_intent_detected: bool | None = None

    #: Production architecture (2026-09), Phase 4: whether `node_route_
    #: capability`'s own real interpretation attempt (`interpret_turn`/
    #: `verify()`) succeeded this turn - `None` when never attempted at
    #: all (nothing to interpret, or gated out), `False` when attempted
    #: and the provider was unavailable/returned nothing, `True` when it
    #: produced a real claim. The signal `degraded_disclaimer()` (graph.py)
    #: checks alongside `understanding_used_model` to distinguish "nothing
    #: needed interpreting" from "tried and failed" - never a spurious
    #: disclaimer on an ordinary, fully-deterministic turn.
    interpretation_used_model: bool | None = None
    #: Spelling/domain-word normalization applied to the search query this
    #: turn, and whether the normalized or the literal search was kept.
    query_normalization: dict | None = None
    #: Store-level discovery ("what do you have?"): the merchant's real categories offered as tappable
    #: choices, and why there were none ("empty"/"unavailable"). See `store_discovery.py`.
    category_choices: list[str] | None = None
    #: Basket-level budget of a guided-mission reply (`basket_budget.BudgetView.trace()`): budget, currency, committed,
    #: remaining, verdict, unknown_stages, alternatives_offered_ids, compat_pairs, duplicates_filtered. Log-only like the rest
    #: of the trace (measurable by reading the structured log line and by the tests; not a stored table).
    basket_budget: dict | None = None
    store_discovery: str | None = None

    response_used_model: bool | None = None

    model_calls: int = 0

    _t_start: float = field(default_factory=time.perf_counter, repr=False)
    stage_timings_s: dict[str, float] = field(default_factory=dict)
    total_s: float | None = None

    error: str | None = None

    def mark(self, stage: str, started_at: float) -> None:
        self.stage_timings_s[stage] = round(time.perf_counter() - started_at, 6)

    def finish(self) -> None:
        self.total_s = round(time.perf_counter() - self._t_start, 6)
        logger.info("shopper_v2 turn", extra={"trace": self.as_dict()})

    def as_dict(self) -> dict:
        return {
            "turn_id": self.turn_id,
            "session_id": self.session_id,
            "connection_id": self.connection_id,
            "capability": self.capability,
            "understanding_used_model": self.understanding_used_model,
            "understanding_model_name": self.understanding_model_name,
            "state_version": self.state_version,
            "search_intent_query": self.search_intent_query,
            "search_intent_fallback_used": self.search_intent_fallback_used,
            "candidate_count": self.candidate_count,
            "candidate_set_truncated": self.candidate_set_truncated,
            "eligible_count": self.eligible_count,
            "ranked_count": self.ranked_count,
            "ranking_confident": self.ranking_confident,
            "llm_judge_used": self.llm_judge_used,
            "llm_judge_result": self.llm_judge_result,
            "llm_judge_pick_passed_hard_filter": self.llm_judge_pick_passed_hard_filter,
            "verified_count": self.verified_count,
            "no_match_reason": self.no_match_reason,
            "semantic_retrieval_tried": self.semantic_retrieval_tried,
            "semantic_retrieval_candidates": self.semantic_retrieval_candidates,
            "semantic_retrieval_verified": self.semantic_retrieval_verified,
            "cart_intent_checked": self.cart_intent_checked,
            "cart_intent_detected": self.cart_intent_detected,
            "interpretation_used_model": self.interpretation_used_model,
            "query_normalization": self.query_normalization,
            "category_choices": list(self.category_choices) if self.category_choices else [],
            "store_discovery": self.store_discovery,
            "basket_budget": self.basket_budget,
            "response_used_model": self.response_used_model,
            "model_calls": self.model_calls,
            "stage_timings_s": dict(self.stage_timings_s),
            "total_s": self.total_s,
            "error": self.error,
        }
