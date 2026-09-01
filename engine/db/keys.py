"""Minting and verifying API keys.

Separate from the repository because this is the only place that touches raw key
material, and that is worth being able to point at.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from .models import ApiKey
from .session import session_scope

#: The prefix tells you what a key is at a glance, which matters when somebody
#: pastes one into a bug report and needs to know how urgently to rotate it.
PREFIXES = {
    "publishable": "cv3_pk_",
    "secret": "cv3_sk_",
    "operator": "cv3_op_",
}


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def _generate(kind: str) -> str:
    """A new key. 32 bytes of urandom, hex encoded.

    urandom rather than the random module: one is seeded from the operating
    system's entropy pool and the other is a deterministic generator that would
    make every key predictable from any other.
    """
    return PREFIXES[kind] + os.urandom(32).hex()


async def mint(
    kind: str,
    *,
    connection_id: str | None = None,
    label: str = "",
    allowed_origins: list[str] | None = None,
) -> str:
    """Create a key and return it once.

    The plain key is returned here and nowhere else, ever. After this call only the
    hash exists, so losing it means minting another - which is the correct trade and
    the reason every provider does it this way.
    """
    if kind not in PREFIXES:
        raise ValueError(f"unknown key kind {kind!r}")

    if kind == "operator" and connection_id is not None:
        raise ValueError("an operator key spans merchants and takes no connection")

    if kind != "operator" and not connection_id:
        raise ValueError(f"a {kind} key must name the merchant it speaks for")

    key = _generate(kind)

    async with session_scope() as db:
        db.add(
            ApiKey(
                key_id=f"key_{uuid.uuid4().hex[:16]}",
                prefix=key[:12],
                key_hash=_hash(key),
                kind=kind,
                connection_id=connection_id,
                allowed_origins=allowed_origins or [],
                label=label,
            )
        )

    return key


async def resolve(key: str) -> ApiKey | None:
    """Find the record for a key, or None.

    Looked up by prefix and confirmed by hash. A prefix collision is possible in
    principle and harmless - the hash comparison decides.

    Returns None for a revoked key rather than raising, so a caller cannot tell a
    revoked key from a fabricated one. That is deliberate: distinguishing them tells
    an attacker which of their guesses was once real.
    """
    if not key or len(key) < 12:
        return None

    async with session_scope() as db:
        rows = await db.execute(
            select(ApiKey).where(ApiKey.prefix == key[:12])
        )
        candidates = list(rows.scalars())

        wanted = _hash(key)
        for row in candidates:
            if row.key_hash == wanted:
                if row.revoked_at is not None:
                    return None
                # Recorded so an operator can see which keys are live. Best effort:
                # a failed write here must not fail the request.
                row.last_used_at = datetime.now(UTC)
                return row

    return None


async def list_keys(connection_id: str | None = None) -> list[dict]:
    """Every key, without the material. For a console listing."""
    async with session_scope() as db:
        query = select(ApiKey)
        if connection_id is not None:
            query = query.where(ApiKey.connection_id == connection_id)
        rows = await db.execute(query)

        return [
            {
                "key_id": r.key_id,
                "prefix": r.prefix,
                "kind": r.kind,
                "connection_id": r.connection_id,
                "label": r.label,
                "allowed_origins": r.allowed_origins or [],
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "last_used_at": (
                    r.last_used_at.isoformat() if r.last_used_at else None
                ),
                "revoked": r.revoked_at is not None,
            }
            for r in rows.scalars()
        ]


async def revoke(key_id: str) -> bool:
    """Turn a key off now. Returns whether anything changed."""
    async with session_scope() as db:
        row = await db.get(ApiKey, key_id)
        if row is None or row.revoked_at is not None:
            return False
        row.revoked_at = datetime.now(UTC)
        return True
