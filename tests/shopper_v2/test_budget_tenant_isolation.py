"""Tenant and session isolation of the basket budget (AC-13).

One session's budget and mission items are invisible to another session, and to the SAME session id under another connection
(merchant). Another merchant's candidate ids never enter this mission's estimate or alternatives, and there is no module-level
cache of budget or prices that could leak between sessions.

    python tests/shopper_v2/test_budget_tenant_isolation.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_tenant.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_tenant.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402
from decimal import Decimal  # noqa: E402

from _budget_fixtures import FakeShop, product, standard_catalogue, start_mission, stored_total  # noqa: E402

failures = 0
MSG = "I want shoes, socks and something for energy, total budget 8000"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.db import shopping_state as store
    from engine.shopper_v2 import basket_budget as bb

    print("1. Session A's budget is invisible to session B and to the same session id under another merchant")
    shop_a = FakeShop(standard_catalogue())
    sid_a, _spec, turn_a = await start_mission(shop_a, MSG, conn="conn_demo")
    row_a = await store.get(sid_a, "conn_demo")
    check("precondition: A's budget is stored for (A, conn_demo)", stored_total(row_a) == 8000, row_a and stored_total(row_a))
    other_conn = await store.get(sid_a, "conn_kettle")
    check("the SAME session id under another connection sees no budget and no mission", other_conn is None or (stored_total(other_conn) is None
          and not other_conn.get("mission_items")), other_conn and stored_total(other_conn))
    shop_b = FakeShop(standard_catalogue())
    sid_b, _s, turn_b = await start_mission(shop_b, "I want shoes, socks and something for energy", conn="conn_demo")
    row_b = await store.get(sid_b, "conn_demo")
    check("session B (no budget stated) has none stored", stored_total(row_b) is None, row_b and stored_total(row_b))
    check("B's reply says nothing about a budget even though A's budget was set in the same process",
          turn_b is not None and "budget" not in turn_b.reply.lower() and turn_b.budget_trace is None, turn_b and turn_b.reply[-200:])

    print("\n2. Another merchant's products never enter this mission")
    kettle = FakeShop([product("KB-1", "Ethiopia Running Shoes Blend", 650), product("KB-2", "House Socks Blend", 500), product("KB-3", "Energy Roast", 420)])
    sid_k, _sk, turn_k = await start_mission(kettle, MSG, sid=sid_a, conn="conn_kettle")
    items_a = (await store.get(sid_a, "conn_demo"))["mission_items"]
    items_k = (await store.get(sid_a, "conn_kettle"))["mission_items"]
    ids_a = {c for i in items_a for c in i["candidates"]}
    ids_k = {c for i in items_k for c in i["candidates"]}
    check("each merchant's mission holds only its own candidate ids", ids_a and ids_k and not (ids_a & ids_k) and all(i.startswith(("S-", "K-", "E-")) for i in ids_a),
          (sorted(ids_a), sorted(ids_k)))
    tr_k = turn_k.budget_trace or {}
    check("the second merchant's alternatives (if any) come only from its own candidates", all(i.startswith("KB-") for i in tr_k.get("alternatives_offered_ids", [])),
          tr_k.get("alternatives_offered_ids"))
    check("the two sessions' stored budgets are independent after the second start", stored_total(await store.get(sid_a, "conn_demo")) == 8000)

    print("\n3. No module-level cache of budget or prices")
    names = [n for n in dir(bb) if not n.startswith("__")]
    mutable = [n for n in names if isinstance(getattr(bb, n), (dict, list, set)) and n.isupper() is False and not n.startswith("_")]
    check("basket_budget keeps no module-level mutable state", mutable == [], mutable)


async def main():
    from engine.db import create_schema, dispose

    await create_schema()
    try:
        await checks()
    finally:
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
