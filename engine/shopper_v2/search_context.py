"""Read/write for `SearchContextV2` - see `db_models.py` for why this table
exists (Stage 2A Finding 4). Deliberately minimal: one nullable string, no
lock (a lost race here just means the next turn's fallback is one turn
stale, not a data-integrity bug the way concurrent `shopping_states` writes
were - see `engine/db/shopping_state.py`'s own lock for that real case)."""

from __future__ import annotations

from engine.db import session_scope

from .db_models import SearchContextV2


async def get_last_effective_query(session_id: str, connection_id: str) -> str | None:
    async with session_scope() as db:
        row = await db.get(SearchContextV2, (session_id, connection_id))
        return row.last_effective_query if row else None


async def set_last_effective_query(
    session_id: str, connection_id: str, query: str
) -> None:
    if not query or not query.strip():
        return
    async with session_scope() as db:
        row = await db.get(SearchContextV2, (session_id, connection_id))
        if row is None:
            row = SearchContextV2(session_id=session_id, connection_id=connection_id)
            db.add(row)
        row.last_effective_query = query.strip()
