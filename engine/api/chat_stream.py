"""POST /api/v2/chat/stream - one Shopper V2 turn as Server-Sent Events.

The SAME turn as `POST /api/v2/chat`: this module authorises with `chat_v2.authorize_turn` and runs `chat_v2.execute_turn`, the one implementation of a V2 turn, so
Decision -> Risk Gate -> Execution -> Adapter, the typed-add and ambiguity rules, ownership checks and every payment guard are unchanged code. It adds a transport, not a
capability: it imports nothing from the payment or idempotency modules, never calls a pay route, and a checkout offer reaches the shopper only as the final `message`.

Wire format (standard SSE: `id:`, `event:`, `data:` single-line JSON, `: ping` comments). The client is `fetch` + `ReadableStream`, because `EventSource` cannot POST or
send an Authorization header. `Last-Event-ID` on this POST is a CUSTOM request header with the standard meaning.

    start    {request_id}
    status   {stage, label}                   whitelisted progress (engine/shopper_v2/streaming.py)
    tool     {name, state}                    "catalogue" | "cart" | "orders", started | finished - never arguments or results
    delta    {segment, text}                  a PROVISIONAL draft of model-written text, released only under the engine's own check for that text
    reset    {segment, reason}                discard the draft (held back by the screen, ungrounded citation, or the model failed)
    message  {...ChatReplyV2, draft_replaced, streamed_chars}      the AUTHORITATIVE reply - exactly what POST /api/v2/chat returns
    error    {code, message, retryable}       fixed text only; the exception is logged server-side
    done     {first_text_ms, total_ms}        always last

Runs. A turn executes at most ONCE per (connection_id, session_id, request_id). It runs in a background task owned by the run (held by the registry with a strong
reference, with terminal events guaranteed), NOT by the HTTP request: a disconnect, a closed tab or a "Stop" never cancels it, because it may be mid-mutation (an explicit
typed add) and a graph node interrupted mid-write could leave partial state. The run finishes, writes the transcript once, and any reader - the same client reconnecting
with the same request_id and Last-Event-ID, or the existing transcript poll - sees the result. Readers hold a cursor over the run's event list (never a blocking queue), so a
slow reader cannot stall the turn. Runs live in this process's memory (single engine process; a restart loses replay, not the transcript).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import Field

from engine import db
from engine.reasoning.stream_sink import REPLY_SINK
from engine.shopper_v2.streaming import TURN_EVENTS, TurnStreamSink

from .auth import Visitor, any_key, visitor
from .chat_v2 import ChatRequestV2, authorize_turn, execute_turn

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/chat", tags=["shopper-v2"])

# Operational limits (module attributes so tests can shrink them).
HEARTBEAT_SECONDS = 15.0
IDLE_TIMEOUT_SECONDS = 120.0
MAX_RUNS = 500
MAX_RUNS_PER_OWNER = 30
MAX_EVENTS = 2000
RUN_TTL_SECONDS = 300.0
# How long a turn may run before the stream stops waiting for it and frees the session. The turn itself is NEVER cancelled (it may be mid-mutation):
# it keeps running and, if it completes, writes its transcript as usual.
TURN_TIMEOUT_SECONDS = 150.0

TURN_FAILED_MESSAGE = "Something went wrong with that message. Please try again."
TURN_TIMEOUT_MESSAGE = "That is taking longer than expected. If it finishes, the reply will appear in this conversation."


class StreamChatRequest(ChatRequestV2):
    """`ChatRequestV2` plus the client's id for this turn. A new model, so the non-streaming route's schema is untouched."""

    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,64}$")


@dataclass
class StreamEvent:
    id: int
    type: str
    data: dict


@dataclass
class TurnRun:
    key: tuple[str, str, str]
    owner_keys: frozenset[str]
    body_hash: str
    events: list[StreamEvent] = field(default_factory=list)
    status: str = "running"
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    task: asyncio.Task | None = None
    _waiters: list[asyncio.Future] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status == "running"

    def emit(self, event_type: str, data: dict) -> None:
        """Append an event (synchronous: it assigns the id and wakes readers). Past the event cap only progress and draft events are dropped -
        a reset, the message, an error and done are never lost."""
        if len(self.events) >= MAX_EVENTS and event_type in {"delta", "status", "tool"}:
            return
        self.events.append(StreamEvent(len(self.events) + 1, event_type, {"type": event_type, **data}))
        for waiter in self._waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def wait_for_more(self, timeout: float) -> bool:
        """Wait until something is emitted (True) or the timeout passes (False)."""
        waiter = asyncio.get_running_loop().create_future()
        self._waiters.append(waiter)
        try:
            await asyncio.wait_for(waiter, timeout)
            return True
        except asyncio.TimeoutError:
            return False
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    @property
    def last_id(self) -> int:
        return self.events[-1].id if self.events else 0


class RunRegistry:
    """Bounded registry of streaming runs: an in-process hot path over a DURABLE record (`db.stream_runs`).

    The durable row is committed BEFORE a turn starts, so after any restart the engine can still say whether a request id was ever started, by whom, for which
    exact message, and what it ended with:
      - finished ("done"/"error"): replayed from the stored events (provisional drafts are not stored), the turn is never run again;
      - never finished ("running" with no live run in this process = INTERRUPTED): refused `TURN_INTERRUPTED`, never run again - it may or may not have changed
        the cart or written the transcript, so the shopper decides whether to send the message again (a new request id);
      - not found: a first attempt runs, unless the client says it already received events (`resuming`) in which case the run is gone and is refused `STREAM_RUN_LOST`.
    `acquire` runs under one lock, so two simultaneous posts of the same request id can never both create it. If the durable store cannot be used the request is refused
    (`STREAM_UNAVAILABLE`): running a turn with no record is how a duplicate happens.
    """

    def __init__(self) -> None:
        self._runs: dict[tuple[str, str, str], TurnRun] = {}
        self._active: dict[tuple[str, str], tuple[str, str, str]] = {}
        self._lock = asyncio.Lock()
        self._last_purge = 0.0

    def clear(self) -> None:
        """Forget the in-memory runs (what an engine restart does). The durable record is untouched."""
        self._runs.clear()
        self._active.clear()

    def _sweep(self) -> None:
        cutoff = time.monotonic() - RUN_TTL_SECONDS
        for key in [k for k, r in self._runs.items() if r.finished_at is not None and r.finished_at < cutoff]:
            del self._runs[key]

    @staticmethod
    def _not_found() -> HTTPException:
        # identical to a conversation that does not exist: no oracle for "that request id is real"
        return HTTPException(404, detail={"code": "SESSION_NOT_FOUND", "message": "no such conversation", "retryable": False})

    @staticmethod
    def _unavailable() -> HTTPException:
        return HTTPException(503, detail={"code": "STREAM_UNAVAILABLE", "message": "streaming is unavailable right now; try again in a moment", "retryable": True})

    async def acquire(self, key: tuple[str, str, str], owner_keys: list[str], body_hash: str, *, resuming: bool = False) -> tuple[TurnRun, bool]:
        """Attach to the run for `key`, rebuild it from the durable record, or create it. Returns (run, created). Raises HTTPException for the refusals."""
        async with self._lock:
            return await self._acquire_locked(key, owner_keys, body_hash, resuming)

    async def _acquire_locked(self, key, owner_keys, body_hash, resuming):
        self._sweep()
        owners = frozenset(owner_keys)
        existing = self._runs.get(key)
        if existing is not None:
            if not (owners & existing.owner_keys):
                raise self._not_found()
            if existing.body_hash != body_hash:
                raise HTTPException(409, detail={"code": "REQUEST_ID_REUSED", "message": "that request id was already used for a different message", "retryable": False})
            return existing, False

        try:
            row = await db.stream_runs.get(*key)
        except Exception:  # noqa: BLE001 - fail closed, details are logged
            logger.exception("stream run record unreadable (%s)", key[:2])
            raise self._unavailable() from None
        if row is not None:
            if not (owners & set(row["owner_keys"])):
                raise self._not_found()
            if row["body_hash"] != body_hash:
                raise HTTPException(409, detail={"code": "REQUEST_ID_REUSED", "message": "that request id was already used for a different message", "retryable": False})
            if row["status"] in {"done", "error"}:
                run = TurnRun(key=key, owner_keys=frozenset(row["owner_keys"]), body_hash=row["body_hash"], status="done", finished_at=time.monotonic())
                run.events = [StreamEvent(e["id"], e["type"], e["data"]) for e in row["events"]]
                self._runs[key] = run
                return run, False
            # "running" with no live run in this process (the live ones were found above), or already marked: an interrupted turn
            try:
                await db.stream_runs.mark_interrupted(*key)
            except Exception:  # noqa: BLE001
                logger.exception("could not mark stream run interrupted (%s)", key[:2])
            raise HTTPException(409, detail={"code": "TURN_INTERRUPTED", "message": "that message was interrupted before it finished", "retryable": False})

        if resuming:
            raise HTTPException(409, detail={"code": "STREAM_RUN_LOST", "message": "that reply was interrupted and can no longer be resumed", "retryable": False})
        session_key = (key[0], key[1])
        active_key = self._active.get(session_key)
        if active_key is not None and active_key in self._runs and self._runs[active_key].active:
            raise HTTPException(409, detail={"code": "TURN_IN_PROGRESS", "message": "that conversation is still answering the last message", "retryable": True})
        mine = sum(1 for r in self._runs.values() if r.active and owners & r.owner_keys)
        if len(self._runs) >= MAX_RUNS or mine >= MAX_RUNS_PER_OWNER:
            raise HTTPException(429, detail={"code": "STREAM_BUSY", "message": "too many conversations are streaming right now; try again in a moment", "retryable": True})
        try:
            created = await db.stream_runs.begin(key[0], key[1], key[2], owner_keys, body_hash)
        except Exception:  # noqa: BLE001
            logger.exception("could not record stream run (%s)", key[:2])
            raise self._unavailable() from None
        if not created:  # someone else committed this id between our read and our insert: never ours to run
            winner = await db.stream_runs.get(*key)
            if winner is not None and not (owners & set(winner["owner_keys"])):
                raise self._not_found()  # same answer as a conversation that does not exist
            raise HTTPException(409, detail={"code": "TURN_IN_PROGRESS", "message": "that conversation is still answering the last message", "retryable": True})
        run = TurnRun(key=key, owner_keys=owners, body_hash=body_hash)
        self._runs[key] = run
        self._active[session_key] = key
        await self._maybe_purge()
        return run, True

    async def _maybe_purge(self) -> None:
        if time.monotonic() - self._last_purge < 3600:
            return
        self._last_purge = time.monotonic()
        try:
            await db.stream_runs.purge()
        except Exception:  # noqa: BLE001 - housekeeping only
            logger.exception("could not purge old stream runs")

    async def finish(self, run: TurnRun) -> None:
        """Mark the run ended and store how it ended. A storage failure is logged and leaves the row "running", which a later request reads as INTERRUPTED -
        the safe reading (never re-run)."""
        if run.status == "done":
            return
        run.status = "done"
        run.finished_at = time.monotonic()
        session_key = (run.key[0], run.key[1])
        if self._active.get(session_key) == run.key:
            del self._active[session_key]
        outcome = "done" if any(e.type == "message" for e in run.events) else "error"
        try:
            await db.stream_runs.complete(
                run.key[0], run.key[1], run.key[2], outcome,
                [{"id": e.id, "type": e.type, "data": e.data} for e in run.events],
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not store the outcome of stream run (%s)", run.key[:2])


REGISTRY = RunRegistry()


def _body_hash(req: StreamChatRequest) -> str:
    """Covers EVERY field that changes what the turn does, so a tap-with-variant retry can never attach to a typed-text run."""
    material = json.dumps(
        [req.message, req.friction, req.friction_query, req.cart_id, req.selected_variant_id],
        separators=(",", ":"),
        sort_keys=False,
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _last_event_id(request: Request) -> int:
    raw = request.headers.get("last-event-id", "")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _format(event: StreamEvent) -> bytes:
    return f"id: {event.id}\nevent: {event.type}\ndata: {json.dumps(event.data, separators=(',', ':'), ensure_ascii=False)}\n\n".encode()


async def _drive(run: TurnRun, req: StreamChatRequest, adapter, who: Visitor) -> None:
    """The run's own task: execute the turn once, stream its progress, and ALWAYS finish with terminal events."""
    sink = TurnStreamSink(run.emit)
    TURN_EVENTS.set(sink)
    REPLY_SINK.set(sink)

    def done_payload() -> dict:
        now = time.monotonic()
        first = None if sink.first_text_at is None else int((sink.first_text_at - run.started_at) * 1000)
        return {"first_text_ms": first, "total_ms": int((now - run.started_at) * 1000)}

    try:
        inner = asyncio.ensure_future(execute_turn(req, adapter, who))
        finished, _ = await asyncio.wait({inner}, timeout=TURN_TIMEOUT_SECONDS)
        if not finished:
            # Stop WAITING, not working: freeing the session lets the shopper send again, while the turn runs on to its own end.
            def _log_late(task: asyncio.Task) -> None:
                if not task.cancelled() and task.exception() is not None:
                    logger.error("streaming turn failed after the stream gave up on it (%s)", run.key[:2], exc_info=task.exception())

            inner.add_done_callback(_log_late)
            run.emit("error", {"code": "TURN_TIMEOUT", "message": TURN_TIMEOUT_MESSAGE, "retryable": False})
            run.emit("done", done_payload())
            await REGISTRY.finish(run)
            return
        reply = inner.result()
        data = reply.model_dump(mode="json")
        draft = sink.draft_text()
        data["draft_replaced"] = bool(draft) and draft not in (reply.reply or "")
        data["streamed_chars"] = sink.streamed_chars
        run.emit("message", data)
    except asyncio.CancelledError:
        run.emit("error", {"code": "TURN_FAILED", "message": TURN_FAILED_MESSAGE, "retryable": True})
        run.emit("done", done_payload())
        await REGISTRY.finish(run)
        raise
    except Exception:  # noqa: BLE001 - the exception text is logged, never sent
        logger.exception("streaming turn failed (%s)", run.key[:2])
        run.emit("error", {"code": "TURN_FAILED", "message": TURN_FAILED_MESSAGE, "retryable": True})
    run.emit("done", done_payload())
    await REGISTRY.finish(run)


async def _sse(run: TurnRun, after: int):
    """Replay events after `after`, then follow the run live. Ends after `done`, or - when nothing happens for the idle timeout - without a
    terminal event, so the client re-attaches with Last-Event-ID."""
    cursor = after
    last_activity = time.monotonic()
    while True:
        pending = [e for e in run.events if e.id > cursor]
        for event in pending:
            yield _format(event)
            cursor = event.id
            last_activity = time.monotonic()
            if event.type == "done":
                return
        if pending:
            continue  # events may have arrived while we were yielding: look again before waiting
        if run.status == "done" and cursor >= run.last_id:
            return
        if await run.wait_for_more(HEARTBEAT_SECONDS):
            continue
        if time.monotonic() - last_activity >= IDLE_TIMEOUT_SECONDS:
            return
        yield b": ping\n\n"


@router.post("/stream")
async def chat_v2_stream(
    req: StreamChatRequest,
    request: Request,
    response: Response,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> StreamingResponse:
    """One shopper turn as SSE. Authorisation is identical to `/api/v2/chat` and happens BEFORE the stream starts, so a refusal is an ordinary HTTP error."""
    adapter = await authorize_turn(req, key, who)
    owner_keys = await who.keys_for(req.connection_id)
    run, created = await REGISTRY.acquire((req.connection_id, req.session_id, req.request_id), owner_keys, _body_hash(req), resuming=_last_event_id(request) > 0)
    if created:
        run.emit("start", {"request_id": req.request_id})
        # strong reference on the run: the loop only holds tasks weakly
        run.task = asyncio.create_task(_drive(run, req, adapter, who))
    stream = StreamingResponse(
        _sse(run, _last_event_id(request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
    # `visitor()` mints a cookie on the injected `response`, which FastAPI drops when a route returns its own response object. Without this a cold
    # visitor's session would be claimed under a key from a cookie their browser never receives.
    for cookie in response.headers.getlist("set-cookie"):
        stream.headers.append("set-cookie", cookie)
    return stream
