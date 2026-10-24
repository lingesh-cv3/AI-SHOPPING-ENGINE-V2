"""What a streaming Shopper V2 turn sends to the shopper while it runs: whitelisted progress, and a gated draft of model-written text.

Two kinds of event come out of here, and neither can carry anything the shopper should not see.

PROGRESS (`status`, `tool`). Emitted when a graph node starts and finishes, mapped through `_STAGES` below - a fixed whitelist of stage ids, shopper-safe labels and
three tool names (`catalogue`, `cart`, `orders`). No node name, argument, result, SQL, platform text or reasoning ever appears: a node that is not in the map says
nothing. A tool event is "something is being looked up", not what it returned.

DRAFT TEXT (`delta`, `reset`). Real tokens of the model's reply, forwarded while the model is still writing - but ONLY where the engine itself shows model text, and only
under the engine's own rule for that text. The reply a shopper finally reads is always the one the turn returns (the `message` event); a draft is provisional.
  - Discovery text is never shown directly (the engine writes the shopper-facing sentence from a validated `cited_reason`, or a deterministic fallback), so a discovery
    call never reaches this sink: nothing is streamed for it.
  - A comparison / recommendation call is shown only if its `cited_reason` is found in the real products' own text (or absent), then screened for cart claims
    (`respond.grounded_reply_or_fallback`). The gate applies THE SAME `respond.cited_reason_supported` BEFORE releasing any text: the tool variant for a streaming call
    lists the grounding fields first, so a model that follows the order has been checked before its first word reaches the screen. Text written before the citation is
    known is buffered, and released in one piece only if the citation passes; an ungrounded citation drops the draft entirely.
  - A compatibility call is shown after only the cart-claim screen, so its draft gets exactly that screen.
  - Every released draft is screened as it grows: a cart / order / payment claim (the engine's own phrase list plus a few more words) emits ONE `reset` and stops the draft.
  - Only whole words are released (the unfinished tail waits for the next space), so a claim cannot be half-shown; text is never rewritten in flight.
  - `CHAT_STREAM_DRAFT_TEXT=off` turns drafts off: progress and the final message only.
If the model never writes the citation first, or the call fails, the draft simply does not appear (or is `reset`); the final message is unaffected.
"""

from __future__ import annotations

import functools
import inspect
import logging
import os
import re
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: node name -> (stage id, shopper-facing label, tool name or None). A node missing from this map emits nothing.
_STAGES: dict[str, tuple[str, str, str | None]] = {
    "load_context": ("understanding", "Reading the conversation", None),
    "understand": ("understanding", "Understanding your message", None),
    "apply_delta": ("understanding", "Understanding your message", None),
    "topic_transition": ("understanding", "Understanding your message", None),
    "route_capability": ("understanding", "Understanding your message", None),
    "reference_resolution": ("understanding", "Understanding your message", None),
    "fulfillment_continue": ("cart", "Working on your cart request", "cart"),
    "guided_turn": ("cart", "Working on your list", "cart"),
    "typed_confirm": ("cart", "Working on your cart request", "cart"),
    "cart_proposal_continue": ("cart", "Working on your cart request", "cart"),
    "mission": ("cart", "Working on your list", "catalogue"),
    "cart_action": ("cart", "Working on your cart request", "cart"),
    "cart_status": ("cart", "Checking your cart", "cart"),
    "cart_intelligence": ("cart", "Checking your cart", "cart"),
    "checkout": ("checkout", "Preparing your checkout summary", "cart"),
    "order_status": ("orders", "Checking your order", "orders"),
    "payment_recovery": ("checking", "Checking that for you", "orders"),
    "tradeoff": ("comparing", "Comparing the options", "catalogue"),
    "compare": ("comparing", "Comparing the options", "catalogue"),
    "answer_question": ("comparing", "Looking into that", "catalogue"),
    "compatibility": ("searching", "Looking through the catalogue", "catalogue"),
    "complementary": ("searching", "Looking through the catalogue", "catalogue"),
    "discovery": ("searching", "Looking through the catalogue", "catalogue"),
    "friction_recovery": ("checking", "Working on that", None),
    "conversational": ("checking", "Working on that", None),
    "unimplemented": ("checking", "Working on that", None),
    "respond": ("writing", "Writing the reply", None),
    "finalize": ("writing", "Writing the reply", None),
}

#: Words that make a draft a cart / order / payment claim, on top of the engine's own phrase list (`respond._CART_CLAIM_PHRASES`).
_CLAIM_WORDS = re.compile(
    r"\b(?:added|removed|ordered|purchased|paid|charged|refunded|"
    r"(?:in|to|from) your (?:cart|basket|bag)|your (?:order|cart|basket))\b",
    re.IGNORECASE,
)


def draft_text_enabled() -> bool:
    """Drafts are on unless CHAT_STREAM_DRAFT_TEXT is set to off/0/false/no."""
    return os.environ.get("CHAT_STREAM_DRAFT_TEXT", "on").strip().lower() not in {"off", "0", "false", "no"}


@dataclass
class _Segment:
    policy: str
    haystack: str
    state: str = "pending"  # pending (waiting for the citation) | released | blocked
    buffer: list[str] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    tail: str = ""  # the unfinished last word, held until a space arrives


class TurnStreamSink:
    """One per streaming run. Implements `engine.reasoning.stream_sink.ReplySink` and the progress events.

    `emit(event_type, data)` is supplied by the run and must be a cheap synchronous append (it assigns the event id); it is the ONLY way anything leaves this object.
    """

    def __init__(self, emit: Callable[[str, dict], None], *, draft_enabled: bool | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._emit = emit
        self._draft = draft_text_enabled() if draft_enabled is None else draft_enabled
        self._clock = clock
        self._segments: dict[int, _Segment] = {}
        self._next_segment = 0
        self._last_stage: str | None = None
        self.first_text_at: float | None = None
        self.streamed_chars = 0
        self.resets = 0

    # ---- progress -------------------------------------------------------------------------------------------------------

    def node_started(self, name: str) -> None:
        stage = _STAGES.get(name)
        if stage is None:
            return
        stage_id, label, tool = stage
        if stage_id != self._last_stage:
            self._last_stage = stage_id
            self._emit("status", {"stage": stage_id, "label": label})
        if tool:
            self._emit("tool", {"name": tool, "state": "started"})

    def node_finished(self, name: str) -> None:
        stage = _STAGES.get(name)
        if stage is not None and stage[2]:
            self._emit("tool", {"name": stage[2], "state": "finished"})

    # ---- draft text (the ReplySink contract) ------------------------------------------------------------------------------

    def begin(self, policy: str, items: list[dict]) -> int:
        from .respond import products_haystack

        self._next_segment += 1
        segment = self._next_segment
        seg = _Segment(policy=policy, haystack=products_haystack(items or []))
        if policy == "cart_claim_screen_only":
            seg.state = "released"  # shown as written after only the cart-claim screen: the draft gets exactly that screen
        self._segments[segment] = seg
        return segment

    async def field(self, segment: int, name: str, value: str | None) -> None:
        seg = self._segments.get(segment)
        if seg is None or name != "cited_reason" or seg.state != "pending":
            return
        from .respond import cited_reason_supported

        if value is not None and not cited_reason_supported(value, seg.haystack):
            self._block(seg, segment, "ungrounded")  # the engine would replace this text with its fallback
            return
        seg.state = "released"
        buffered, seg.buffer = "".join(seg.buffer), []
        if buffered and self._draft:
            self._release(seg, segment, buffered)  # one coalesced piece: it was written before the citation could be checked

    async def delta(self, segment: int, text: str) -> None:
        seg = self._segments.get(segment)
        if seg is None or not text or not self._draft or seg.state == "blocked":
            return
        if seg.state == "pending":
            seg.buffer.append(text)  # not shown until the citation has passed the engine's own check
            return
        self._release(seg, segment, text)

    async def end(self, segment: int, ok: bool) -> None:
        seg = self._segments.get(segment)
        if seg is None:
            return
        if not ok and seg.released:
            self._block(seg, segment, "model_failed")
        seg.buffer.clear()  # a draft whose citation never arrived is not shown; the final message covers it
        seg.tail = ""

    # ---- internals ------------------------------------------------------------------------------------------------------

    def _unsafe(self, text: str) -> bool:
        from .respond import _claims_a_cart_action

        return _claims_a_cart_action(text) or _CLAIM_WORDS.search(text) is not None

    def _release(self, seg: _Segment, segment: int, text: str) -> None:
        combined = seg.tail + text
        cut = max(combined.rfind(" "), combined.rfind("\n"), combined.rfind("\t"))
        if cut < 0:
            seg.tail = combined  # an unfinished word: hold it
            return
        releasable, seg.tail = combined[: cut + 1], combined[cut + 1 :]
        if self._unsafe("".join(seg.released) + releasable):
            self._block(seg, segment, "held_back")
            return
        seg.released.append(releasable)
        self.streamed_chars += len(releasable)
        if self.first_text_at is None:
            self.first_text_at = self._clock()
        self._emit("delta", {"segment": segment, "text": releasable})

    def _block(self, seg: _Segment, segment: int, reason: str) -> None:
        had_text = bool(seg.released)
        self.streamed_chars -= sum(len(p) for p in seg.released)
        seg.released.clear()
        seg.buffer.clear()
        seg.tail = ""
        seg.state = "blocked"
        if had_text:
            self.resets += 1
            self._emit("reset", {"segment": segment, "reason": reason})

    def draft_text(self) -> str:
        """The released, not-reset draft text so far, segments in order."""
        return " ".join("".join(self._segments[s].released).strip() for s in sorted(self._segments) if self._segments[s].released).strip()


#: The sink for the streaming turn running in THIS task (and the graph-node tasks it creates). None = not streaming.
TURN_EVENTS: ContextVar[TurnStreamSink | None] = ContextVar("turn_events", default=None)


def announce(name: str, fn):
    """Wrap an async graph node so a streaming turn reports it (whitelisted) when it starts and finishes. A non-async node, or no sink, changes nothing."""
    if not inspect.iscoroutinefunction(fn):
        return fn

    @functools.wraps(fn)
    async def wrapped(*args, **kwargs):
        sink = TURN_EVENTS.get()
        if sink is None:
            return await fn(*args, **kwargs)
        try:
            sink.node_started(name)
        except Exception:  # noqa: BLE001 - progress is never allowed to break a turn
            logger.warning("stream progress failed", exc_info=True)
        try:
            return await fn(*args, **kwargs)
        finally:
            try:
                sink.node_finished(name)
            except Exception:  # noqa: BLE001
                logger.warning("stream progress failed", exc_info=True)

    return wrapped
