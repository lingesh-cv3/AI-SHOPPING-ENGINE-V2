"""Durable record of streamed shopper turns (`StreamRun`): the part of the SSE duplicate-turn guard that survives an engine restart.

Small and dumb on purpose: `begin` claims a (connection, session, request id) atomically, `complete` stores how it ended, `get` reads it back, `purge` forgets old
rows. What the rows MEAN (interrupted, replay, refusal) is decided in `engine/api/chat_stream.py`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from .models import StreamRun
from .session import session_scope

#: How long a finished or interrupted run is remembered. A retry of a request id older than this is indistinguishable from a first attempt, so this is the
#: window inside which "already handled" is guaranteed. Request ids are fresh per message and retries happen within minutes; a week is generous.
RETENTION = timedelta(days=7)

#: Events are stored whole up to this many characters of JSON; beyond it only the terminal events (message/error/done) are kept.
MAX_EVENTS_CHARS = 400_000


async def begin(connection_id: str, session_id: str, request_id: str, owner_keys: list[str], body_hash: str) -> bool:
    """Record that this request id has started. True if this call created the row, False if one already existed (the caller then reads it with `get`)."""
    async with session_scope() as db:
        db.add(
            StreamRun(
                run_row_id=f"sr_{uuid.uuid4().hex[:24]}",
                connection_id=connection_id,
                session_id=session_id,
                request_id=request_id,
                owner_keys=sorted(set(owner_keys)),
                body_hash=body_hash,
                status="running",
            )
        )
        try:
            await db.flush()
        except IntegrityError:
            await db.rollback()
            return False
    return True


async def get(connection_id: str, session_id: str, request_id: str) -> dict | None:
    async with session_scope() as db:
        row = (
            await db.execute(
                select(StreamRun).where(
                    StreamRun.connection_id == connection_id,
                    StreamRun.session_id == session_id,
                    StreamRun.request_id == request_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        return {
            "owner_keys": list(row.owner_keys or []),
            "body_hash": row.body_hash,
            "status": row.status,
            "events": list(row.events_json or []),
        }


def _compact(events: list[dict]) -> list[dict]:
    """The events worth replaying after a restart: everything except provisional draft text. Original ids are kept so Last-Event-ID still means the same thing."""
    kept = [e for e in events if e.get("type") != "delta"]
    import json

    if len(json.dumps(kept)) > MAX_EVENTS_CHARS:
        kept = [e for e in kept if e.get("type") in {"start", "message", "error", "done"}]
    return kept


async def complete(connection_id: str, session_id: str, request_id: str, status: str, events: list[dict]) -> None:
    """Store how the run ended. Only moves a "running" row forward, so a late call can never rewrite a finished record."""
    async with session_scope() as db:
        await db.execute(
            update(StreamRun)
            .where(
                StreamRun.connection_id == connection_id,
                StreamRun.session_id == session_id,
                StreamRun.request_id == request_id,
                StreamRun.status == "running",
            )
            .values(status=status, events_json=_compact(events), finished_at=datetime.now(UTC))
        )


async def mark_interrupted(connection_id: str, session_id: str, request_id: str) -> None:
    """A "running" row nobody in this process owns: record it as interrupted (idempotent; finished rows are untouched)."""
    async with session_scope() as db:
        await db.execute(
            update(StreamRun)
            .where(
                StreamRun.connection_id == connection_id,
                StreamRun.session_id == session_id,
                StreamRun.request_id == request_id,
                StreamRun.status == "running",
            )
            .values(status="interrupted", finished_at=datetime.now(UTC))
        )


async def purge(older_than: timedelta = RETENTION) -> int:
    """Forget runs older than the retention window. Returns how many rows went."""
    cutoff = datetime.now(UTC) - older_than
    async with session_scope() as db:
        result = await db.execute(delete(StreamRun).where(StreamRun.created_at < cutoff))
        return result.rowcount or 0
