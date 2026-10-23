"""Tenant and session isolation of announced add-ons and anchors (AC-17).

One session's announced add-ons are invisible to another session and to the SAME session id under another merchant; another merchant's
declared ids never enter this mission's view; a declared id that does not exist for this merchant is skipped, not an error; and the
module keeps no module-level state that could leak between sessions.

    python tests/shopper_v2/test_complete_set_tenant_isolation.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_tenant.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_tenant.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import start_mission  # noqa: E402
from _cs_fixtures import MSG, FakeShop, advance, cs_catalogue, ids, product  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def announced(row):
    return sorted({x for d in (row or {}).get("mission_items", []) for x in d.get("offered_complement_ids", [])})


async def checks():
    from engine.db import shopping_state as store
    from engine.shopper_v2 import mission_complements as mc

    print("1. Session A's announced add-ons are invisible to session B and to the same session id under another merchant")
    shop_a = FakeShop(cs_catalogue())
    sid_a, _s, _t = await start_mission(shop_a, MSG, conn="conn_demo")
    await advance(shop_a, sid_a, "S-A", conn="conn_demo")
    row_a = await store.get(sid_a, "conn_demo")
    check("precondition: A announced the slide (stored with A's own stages)", announced(row_a) == ["X-SLIDE"], announced(row_a))
    check("the SAME session id under another connection sees none of it", announced(await store.get(sid_a, "conn_kettle")) == [])
    shop_b = FakeShop(cs_catalogue())
    sid_b, _b, _bt = await start_mission(shop_b, MSG, conn="conn_demo")
    check("a different session on the same merchant has announced nothing", announced(await store.get(sid_b, "conn_demo")) == [])
    b = await advance(shop_b, sid_b, "S-A", conn="conn_demo")
    check("B is offered the slide itself (A's announcement did not suppress it)", "X-SLIDE" in ids(b[1]), ids(b[1]))

    print("\n2. Another merchant's declared ids never enter this mission")
    other = cs_catalogue()
    next(p for p in other if p.product_id == "S-A").compatible_with = ["NOT-HERE", "K-1"]  # one id that does not exist for THIS merchant
    shop_k = FakeShop(other)
    sid_k, _k, _kt = await start_mission(shop_k, MSG, conn="conn_kettle")
    k = await advance(shop_k, sid_k, "S-A", conn="conn_kettle")
    check("a declared id that does not exist for this merchant is skipped with no error text", "NOT-HERE" not in k[0] and "cannot fetch" not in k[0], k[0][-250:])
    check("this merchant's reply never offers the OTHER merchant's slide (its S-A no longer declares it)", "X-SLIDE" not in ids(k[1]), ids(k[1]))
    check("and the first merchant's stored announcement is untouched", announced(await store.get(sid_a, "conn_demo")) == ["X-SLIDE"])

    print("\n3. No module-level state that could leak between sessions")
    mutable = [n for n in dir(mc) if not n.startswith("__") and isinstance(getattr(mc, n), (dict, list, set))]
    check("mission_complements keeps no module-level mutable state (all per-call caches are local)", mutable == [], mutable)


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
