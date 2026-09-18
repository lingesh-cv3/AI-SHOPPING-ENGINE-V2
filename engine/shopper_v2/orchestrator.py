"""The Stage 1 turn orchestrator. The one place that assembles the whole
loop, and the one place whose CALL ORDER is the actual enforcement of the
core Stage 1 invariant:

    message -> understand -> StateDelta -> ShoppingStateV2 -> context
            -> retrieval -> evidence -> respond

`respond()` is called LAST, and its signature (see `respond.py`) accepts
only the post-retrieval `TurnEvidence`, the real fetched `Product`s, and the
current `ShoppingStateV2` - there is no path through this function that can
reach a response before retrieval and evidence exist.

This module must never import `engine.decision`, `engine.risk`, or
`engine.execution` for anything except the read-only, deterministic
relevance/ranking helpers already used inside `evidence.py`
(`engine.execution.relevance`, `engine.execution.mission_ranking`) - no
`ActionType`, no `ProposedAction`, no adapter mutation method is imported or
called anywhere in this package. See `tests/shopper_v2/test_no_mutation_imports.py`.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.reasoning.service import ReasoningService

from .context import build_turn_context
from .evidence import TurnEvidence, build_evidence
from .respond import respond as respond_
from .retrieval import retrieve
from .state import ShoppingStateV2, apply_delta, load
from .understanding import understand


@dataclass
class TurnResultV2:
    reply: str
    state: ShoppingStateV2
    evidence: TurnEvidence
    used_model: bool


async def run_turn(
    *,
    reasoning: ReasoningService,
    adapter,
    session_id: str,
    connection_id: str,
    message: str,
    memory_facts: list[dict] | None = None,
    catalog_sample: list | None = None,
) -> TurnResultV2:
    """Run one Stage 1 turn, read-only, start to finish."""
    # 1. Load current typed state.
    current_state = await load(session_id, connection_id)

    # 2. Bounded context (history/friction) - existing session store, unmodified.
    turn_context = await build_turn_context(session_id, connection_id)

    # 3. Understanding -> StateDelta. The model's `reply` and `actions` are
    #    discarded inside `understand()` itself - nothing here ever sees them.
    result = await understand(
        reasoning=reasoning,
        message=message,
        history=turn_context.history,
        shopping_state=current_state.as_prompt_dict(),
        memory_facts=memory_facts,
    )
    delta = result.delta

    # 4. Apply the delta -> new typed ShoppingStateV2 (existing persistence
    #    + existing per-session lock, reused unmodified; version incremented
    #    in-memory only - see state.py's own docstring).
    new_state = await apply_delta(current_state, delta)

    # 5. Retrieval - existing adapter.search_products, read-only.
    #
    # Deliberately falls back to the raw shopper MESSAGE, not the stored
    # `goal` - live-verified bug fix: falling back to `goal` meant every
    # later turn in a session re-searched the FIRST turn's goal forever
    # (StateDelta.search_query is not yet populated by anything - see
    # `understanding.py`'s own note), so "under 3000" and "do you sell
    # kayaks?" both silently re-ran the first turn's "running shoes" query
    # instead of what was actually just asked. The raw message is exactly
    # what V1's own "rescue search" fallback already searches on when the
    # model leaves its query blank (see engine/execution/relevance.py's
    # module docstring) - reusing that same default, not inventing a new one.
    query = delta.search_query or message
    retrieval_result = await retrieve(adapter, query)

    # 6. Evidence - wraps the existing relevance/ranking logic.
    evidence = build_evidence(
        query=query,
        raw_said=message,
        products=retrieval_result.products,
        shopping_state=new_state.as_prompt_dict(),
        memory_facts=memory_facts,
        catalog_sample=catalog_sample,
        platform_unavailable=retrieval_result.unavailable,
    )

    # 7. Response - generated ONLY from evidence/products/state. Never from
    #    the pre-fetch understanding reply, which no longer exists by this
    #    point (it was discarded in step 3).
    reply_text, used_model = await respond_(
        reasoning=reasoning,
        message=message,
        evidence=evidence,
        products=retrieval_result.products,
        state=new_state,
    )

    return TurnResultV2(
        reply=reply_text, state=new_state, evidence=evidence, used_model=used_model
    )
