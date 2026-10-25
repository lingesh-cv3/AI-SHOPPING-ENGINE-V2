"""A signed-in shopper can SEE and FORGET what the assistant remembers about them - and only their own, only at this merchant.

Before: durable facts were written from conversation and read into every reply, but there was no way to list or erase one. A wrong or unwanted fact
could only be 'superseded' by saying something new. Now `GET/DELETE /api/account/memory` (handlers called directly, real DB) lists and deletes.

    python tests/shopper_v2/test_shopper_memory_control.py
"""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_memory_control.db")
for _s in ("", "-journal", "-wal", "-shm"):
    if os.path.exists(_DB + _s):
        os.remove(_DB + _s)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_memory_control.db"
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail=None) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + ("" if ok or detail is None else f"   {detail!r}"))
    if not ok:
        failures += 1


async def main() -> None:
    from fastapi import HTTPException

    from engine import db
    from engine.api import accounts
    from engine.db import create_schema, dispose, memory_facts

    await create_schema()
    A, K = "conn_demo", "conn_kettle"
    try:
        ann = (await db.shoppers.create(A, "ann_memory", "correct horse battery"))["shopper_id"]
        bob = (await db.shoppers.create(A, "bob_memory", "correct horse battery"))["shopper_id"]
        ann_k = (await db.shoppers.create(K, "ann_memory", "correct horse battery"))["shopper_id"]
        ann_tok = await db.shopper_sessions.start(ann, A)
        bob_tok = await db.shopper_sessions.start(bob, A)
        pk_a = SimpleNamespace(kind="publishable", connection_id=A)

        await memory_facts.upsert(A, ann, key="prefers_light", value="prefers lightweight gear")
        await memory_facts.upsert(A, ann, key="size", value="wears size 9")
        await memory_facts.upsert(A, bob, key="prefers_light", value="bob prefers cushioned")
        await memory_facts.upsert(K, ann_k, key="prefers_light", value="likes decaf")

        print("1. A signed-in shopper lists their own facts - nobody else's")
        mine = await accounts.memory(A, key=pk_a, cv3_shopper=ann_tok)
        check("signed in, with exactly their two facts", mine["signed_in"] and sorted(f["key"] for f in mine["facts"]) == ["prefers_light", "size"], mine)
        check("no other shopper's value appears", all("bob" not in f["value"] and "decaf" not in f["value"] for f in mine["facts"]))
        guest = await accounts.memory(A, key=pk_a, cv3_shopper=None)
        check("a guest sees signed_in false and an empty list", guest == {"signed_in": False, "facts": []}, guest)
        wrong_merchant = None
        try:
            await accounts.memory(K, key=pk_a, cv3_shopper=ann_tok)
        except HTTPException as exc:
            wrong_merchant = exc.status_code
        check("a Northfield key cannot read through the Kettle connection", wrong_merchant == 401, wrong_merchant)
        k_view = await accounts.memory(K, key=SimpleNamespace(kind="publishable", connection_id=K), cv3_shopper=ann_tok)
        check("a Northfield session is not signed in at Kettle (accounts are per merchant)", k_view == {"signed_in": False, "facts": []}, k_view)

        print("2. Forgetting one fact removes only that fact")
        out = await accounts.forget_memory(A, fact_key="size", key=pk_a, cv3_shopper=ann_tok)
        check("one row went", out == {"forgotten": 1}, out)
        left = await accounts.memory(A, key=pk_a, cv3_shopper=ann_tok)
        check("the other fact is still there", [f["key"] for f in left["facts"]] == ["prefers_light"], left)
        check("the model-facing read no longer returns it", all(f["key"] != "size" for f in await memory_facts.for_shopper(A, ann)))

        print("3. Isolation: a key that is someone else's, or another merchant's, is not found")
        out = await accounts.forget_memory(A, fact_key="prefers_light", key=pk_a, cv3_shopper=bob_tok)
        check("bob forgets HIS prefers_light", out == {"forgotten": 1}, out)
        check("ann's same-named fact survived bob's delete", [f["value"] for f in await memory_facts.for_shopper(A, ann)] == ["prefers lightweight gear"])
        check("ann's Kettle fact survived", [f["value"] for f in await memory_facts.for_shopper(K, ann_k)] == ["likes decaf"])
        out = await accounts.forget_memory(A, fact_key="nope", key=pk_a, cv3_shopper=ann_tok)
        check("an unknown key deletes nothing", out == {"forgotten": 0}, out)

        print("4. A guest cannot forget; 'forget everything' clears only this shopper at this merchant")
        refused = None
        try:
            await accounts.forget_memory(A, fact_key=None, key=pk_a, cv3_shopper=None)
        except HTTPException as exc:
            refused = exc.status_code
        check("a guest delete is refused (401)", refused == 401, refused)
        await memory_facts.upsert(A, ann, key="size", value="wears size 9")
        out = await accounts.forget_memory(A, fact_key=None, key=pk_a, cv3_shopper=ann_tok)
        check("everything of ann's at Northfield went", out["forgotten"] == 2 and await memory_facts.for_shopper(A, ann) == [], out)
        check("ann's Kettle fact is untouched by a Northfield wipe", len(await memory_facts.for_shopper(K, ann_k)) == 1)

        print("5. After forgetting, a new statement starts fresh")
        await memory_facts.upsert(A, ann, key="prefers_light", value="prefers durable gear")
        again = await memory_facts.for_shopper(A, ann)
        check("only the new value exists", [f["value"] for f in again] == ["prefers durable gear"], again)
    finally:
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)
