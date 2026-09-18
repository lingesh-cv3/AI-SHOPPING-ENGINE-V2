"""Context assembly for Stage 1.

Delegates entirely to the EXISTING `engine.reasoning.context.build_context`
for the prose contract V1's `ReasoningService.reason()` already consumes -
no reimplementation, no new raw-transcript dumping, no new access to shopper
records beyond what `build_context` already exposes. This module exists only
so `engine/shopper_v2/orchestrator.py` has one place to read for "what goes
into a turn" without reaching into `engine.reasoning` directly for every
field, and so a future Stage can extend the V2-specific context (an evidence
object folded back in, for example) without touching V1's own contract.
"""

from __future__ import annotations

from dataclasses import dataclass

from engine.session.store import context_for as _session_context_for


@dataclass
class TurnContext:
    """Everything Stage 1's Understanding step needs about the conversation
    so far, bounded exactly the same way V1 already bounds it
    (`HISTORY_TURNS` in `engine.session.store`)."""

    history: list[str]
    recorded_friction: list[str]


async def build_turn_context(session_id: str, connection_id: str) -> TurnContext:
    """Read conversation history and recorded friction the same way
    `engine/api/chat.py::_process_turn` already does, via the existing,
    unmodified `session_store.context_for`."""
    history, friction = await _session_context_for(session_id, connection_id)
    return TurnContext(history=history, recorded_friction=friction)
