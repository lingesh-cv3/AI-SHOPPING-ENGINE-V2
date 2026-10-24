"""The opt-in channel through which a streaming turn receives the model's reply text as it is written.

Lives in `engine/reasoning` (not in the shopper package) so the reasoning layer never imports upward. A streaming turn sets `REPLY_SINK` to its own
per-run sink BEFORE it invokes the turn; `ReasoningService.synthesize()` - the only place a model writes shopper-visible text - reads it and, only
when one is set, asks the LLM client to stream. With no sink set (every non-streaming call, V1, every existing test fake) nothing here is touched and
the model request is byte-identical to what it was before streaming existed.

Only `synthesize` may use this. The structured calls (understanding, interpretation, relevance judges) return JSON for the engine to act on; they
are never streamed and never reach the shopper as text.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Protocol


class ReplySink(Protocol):
    """What a streaming turn provides. All methods must be cheap and must never raise into the model call."""

    def begin(self, policy: str, items: list[dict]) -> int:
        """A synthesis call whose text the engine may show is starting. Returns its segment number, assigned at ENTRY so overlapping or
        discarded calls cannot interleave.

        `policy` says how the engine itself treats this model text before the shopper sees it: "cited_reason_check" (shown only if its
        cited reason is found in the real products' text - built from `items` - or absent) or "cart_claim_screen_only". The sink applies the
        same rule BEFORE releasing any draft. A call whose text the engine never shows directly (discovery) never reaches the sink.
        """

    async def field(self, segment: int, name: str, value: str | None) -> None:
        """An internal field of the same tool call (`cited_reason`) is complete: its text, or None for null/non-string. Never shown."""

    async def delta(self, segment: int, text: str) -> None:
        """New decoded characters of the reply text of that segment."""

    async def end(self, segment: int, ok: bool) -> None:
        """The call finished (`ok`) or failed (not `ok`: the draft of that segment must be discarded)."""


#: The sink for the turn running in THIS task (and the tasks it creates). None = not streaming.
REPLY_SINK: ContextVar[ReplySink | None] = ContextVar("reply_sink", default=None)
