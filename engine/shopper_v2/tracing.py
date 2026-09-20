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
    verified_count: int | None = None
    no_match_reason: str | None = None

    #: Phase 4 continuation - whether the general cart-intent interpreter
    #: (`intent_resolution.py`) ran this turn, and what it found. Absent
    #: (both None/False) means it never ran at all (no decision/gate/
    #: execution wired, or nothing currently shown to refer to).
    cart_intent_checked: bool = False
    cart_intent_detected: bool | None = None

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
            "verified_count": self.verified_count,
            "no_match_reason": self.no_match_reason,
            "cart_intent_checked": self.cart_intent_checked,
            "cart_intent_detected": self.cart_intent_detected,
            "response_used_model": self.response_used_model,
            "model_calls": self.model_calls,
            "stage_timings_s": dict(self.stage_timings_s),
            "total_s": self.total_s,
            "error": self.error,
        }
