"""API key resolution must never refuse a good key because a write failed.

Found while browser-verifying Merchant Approvals: under a burst of concurrent
requests the engine logged `sqlite3.OperationalError: database is locked` from
`keys.resolve`, which used to write "last used" inside the same transaction as the
lookup. The failure was caught by `auth._resolved` and read as "no such key", so a
correct key got a 401 - and the console, which forgets a key on any 401, silently
signed the merchant out mid-session.

A locked database is simulated here by making every commit that carries a write
fail, which is what SQLite's single writer does under contention. Reads are not
affected, as in the real failure.

Run from the project root:
    python -m tests.ops.test_key_resolution
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

from tests.ops import _support

_support.isolate("keys")

import httpx  # noqa: E402
from sqlalchemy.exc import OperationalError  # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession  # noqa: E402

from engine import db  # noqa: E402
from engine.api.main import app  # noqa: E402
from engine.db import keys  # noqa: E402
from engine.db.models import ApiKey  # noqa: E402
from engine.db.session import create_schema, dispose, session_scope  # noqa: E402

_support.assert_isolated()

C = _support.Checks()


@contextmanager
def locked_for_writes():
    """Every commit that carries a write fails, as under SQLite write contention."""
    original = AsyncSession.commit

    async def commit(self, *args, **kwargs):
        if self.dirty or self.new or self.deleted:
            raise OperationalError("UPDATE", {}, Exception("database is locked"))
        return await original(self, *args, **kwargs)

    AsyncSession.commit = commit
    try:
        yield
    finally:
        AsyncSession.commit = original


async def last_used(key_id: str):
    async with session_scope() as s:
        return (await s.get(ApiKey, key_id)).last_used_at


async def main() -> int:
    await create_schema()

    sk = await db.keys.mint("secret", connection_id="conn_demo", label="k-secret")
    pk = await db.keys.mint("publishable", connection_id="conn_demo", label="k-pub")
    op = await db.keys.mint("operator", label="k-op")
    dead = await db.keys.mint("secret", connection_id="conn_demo", label="k-dead")
    ids = {r["label"]: r["key_id"] for r in await db.keys.list_keys()}
    await db.keys.revoke(ids["k-dead"])

    print("\n  resolution semantics\n")
    for label, key, kind in (("secret", sk, "secret"), ("publishable", pk, "publishable"),
                             ("operator", op, "operator")):
        rec = await keys.resolve(key)
        C.ok(f"a valid {label} key resolves to its record",
             rec is not None and rec.kind == kind)
    C.ok("a revoked key resolves to nothing", await keys.resolve(dead) is None)
    C.ok("a fabricated key resolves to nothing",
         await keys.resolve("cv3_sk_" + "0" * 64) is None)
    C.ok("a key sharing a valid prefix but not the rest resolves to nothing",
         await keys.resolve(sk[:12] + "x" * (len(sk) - 12)) is None)
    C.ok("empty and short keys resolve to nothing",
         await keys.resolve("") is None and await keys.resolve("cv3_sk_") is None)

    print("\n  a failed write never refuses a good key\n")
    with locked_for_writes():
        for label, key in (("secret", sk), ("publishable", pk), ("operator", op)):
            try:
                rec = await keys.resolve(key)
                ok = rec is not None
                detail = ""
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"raised {type(exc).__name__}"
            C.ok(f"a {label} key still resolves while writes are failing", ok, detail)
        C.ok("a revoked key is still refused while writes are failing",
             await keys.resolve(dead) is None)

        # The same, end to end: what the console actually saw.
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
            r = await http.get("/api/approvals/conn_demo",
                               headers={"Authorization": f"Bearer {sk}"})
            C.ok("GET /api/approvals with a good key is 200 while writes are failing",
                 r.status_code == 200, f"status {r.status_code}")
            bad = await http.get("/api/approvals/conn_demo",
                                 headers={"Authorization": "Bearer cv3_sk_" + "0" * 64})
            C.ok("...and a bad key is still 401 (the fix widens nothing)",
                 bad.status_code == 401)

    print("\n  'last used' is still recorded, at most once a minute\n")
    key_id = ids["k-secret"]
    async with session_scope() as s:
        (await s.get(ApiKey, key_id)).last_used_at = None
    await keys.resolve(sk)
    first = await last_used(key_id)
    C.ok("a use is recorded", first is not None)
    await asyncio.sleep(0.05)
    await keys.resolve(sk)
    C.ok("a second use within a minute does not rewrite it", await last_used(key_id) == first)
    async with session_scope() as s:
        (await s.get(ApiKey, key_id)).last_used_at = datetime.now(UTC) - timedelta(minutes=5)
    await keys.resolve(sk)
    refreshed = await last_used(key_id)
    C.ok("a use after a stale value refreshes it",
         refreshed is not None and (datetime.now(UTC) - refreshed.replace(tzinfo=refreshed.tzinfo or UTC))
         < timedelta(minutes=1))

    await dispose()
    return C.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
