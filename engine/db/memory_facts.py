"""Durable, structured facts about a signed-in shopper - the "important
shopper memory" layer, separate from the current mission (ShoppingState) and
from raw conversation history (SessionTurn).

Authenticated shoppers only. See ShopperMemoryFact's own docstring for why a
guest never gets a row here.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import delete, select

from .models import ShopperMemoryFact
from .session import session_scope

#: How many facts to fold into a turn's context. Small and deliberate - this
#: is meant to be a short list of things that materially help, not a dump of
#: everything ever noticed about a shopper. If a shopper accumulates more
#: than this many durable facts, the most recently updated ones win: a fact
#: touched again recently is more likely to still be live than one untouched
#: for months.
CONTEXT_LIMIT = 8


async def upsert(
    connection_id: str,
    shopper_id: str,
    *,
    key: str,
    value: str,
    source_session_id: str | None = None,
    provenance: str = "shopper_stated",
    confidence: float = 1.0,
) -> None:
    """Record or supersede one durable fact.

    A later call with the same (connection_id, shopper_id, key) replaces the
    value, provenance, confidence and timestamp of the row already there -
    new explicit information supersedes conflicting old information, and
    there is exactly one row per key rather than an accumulating history of
    every version.
    """
    key = key.strip()[:120]
    value = value.strip()
    if not key or not value:
        return

    async with session_scope() as db:
        result = await db.execute(
            select(ShopperMemoryFact).where(
                ShopperMemoryFact.connection_id == connection_id,
                ShopperMemoryFact.shopper_id == shopper_id,
                ShopperMemoryFact.key == key,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            db.add(
                ShopperMemoryFact(
                    fact_id=f"mem_{uuid.uuid4().hex[:16]}",
                    connection_id=connection_id,
                    shopper_id=shopper_id,
                    key=key,
                    value=value,
                    provenance=provenance,
                    confidence=confidence,
                    source_session_id=source_session_id,
                )
            )
            return

        row.value = value
        row.provenance = provenance
        row.confidence = confidence
        row.source_session_id = source_session_id
        row.updated_at = datetime.now(UTC)


async def forget(connection_id: str, shopper_id: str, *, key: str | None = None) -> int:
    """Delete one remembered fact (`key`) or, with no key, all of this shopper's facts at this merchant. Returns how many rows went.

    Scoped by BOTH connection and shopper in the same statement, so a key that exists for somebody else - or at another merchant - is simply not found.
    A deleted fact is gone, not expired: "forget" means the model never sees it again and a later statement starts fresh.
    """
    async with session_scope() as db:
        stmt = delete(ShopperMemoryFact).where(
            ShopperMemoryFact.connection_id == connection_id,
            ShopperMemoryFact.shopper_id == shopper_id,
        )
        if key is not None:
            stmt = stmt.where(ShopperMemoryFact.key == key)
        result = await db.execute(stmt)
        return result.rowcount or 0


async def for_shopper(
    connection_id: str, shopper_id: str, *, limit: int = CONTEXT_LIMIT
) -> list[dict]:
    """The durable facts worth putting in front of the model this turn.

    Expired facts (an `expires_at` in the past) are excluded rather than
    deleted - a fact that stopped being true is still a fact somebody once
    stated, and the row can still answer "what did we used to know" without
    the model ever seeing it as current.
    """
    now = datetime.now(UTC)
    async with session_scope() as db:
        result = await db.execute(
            select(ShopperMemoryFact)
            .where(
                ShopperMemoryFact.connection_id == connection_id,
                ShopperMemoryFact.shopper_id == shopper_id,
            )
            .order_by(ShopperMemoryFact.updated_at.desc())
            .limit(limit * 2)  # headroom before the expiry filter below
        )
        rows = list(result.scalars())

    live = [r for r in rows if r.expires_at is None or r.expires_at > now]
    return [
        {
            "key": r.key,
            "value": r.value,
            "provenance": r.provenance,
            "confidence": r.confidence,
        }
        for r in live[:limit]
    ]
