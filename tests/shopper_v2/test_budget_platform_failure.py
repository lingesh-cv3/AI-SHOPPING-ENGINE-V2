"""A platform that cannot (prices missing) or is down (cart / search / product reads fail): the budget view degrades honestly (AC-11).

An unreadable cart is NOT an empty cart (committed is unknown, never 0); a failed search at the start creates no mission state; a
failed live product read just drops that product from the alternatives; no exception ever reaches the reply and no raw error text
is shown to the shopper.

    python tests/shopper_v2/test_budget_platform_failure.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_platform_failure.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_platform_failure.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import FakeShop, product, standard_catalogue, start_mission  # noqa: E402

failures = 0
MSG = "I want shoes, socks and something for energy, total budget 8000"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.db import shopping_state as store
    from engine.shopper_v2 import guided_mission as g

    print("1. The cart cannot be read when the mission starts")
    shop = FakeShop(standard_catalogue())
    shop.cart_fails = True
    sid, spec, turn = await start_mission(shop, MSG)
    tr = (turn.budget_trace or {}) if turn else {}
    check("the mission still starts and still shows the stage options", turn is not None and "Alpha Running Shoes" in turn.reply, turn and turn.reply[:200])
    check("committed is UNKNOWN (None), not 0", tr.get("committed") is None and tr.get("verdict") == "unknown", tr)
    check("the reply says it could not read the cart (no claim of how much is left)", "couldn't read your cart" in turn.reply and "Good so far" not in turn.reply, turn.reply[-250:])
    check("no raw error text reaches the shopper", "cart unreadable" not in turn.reply and "RuntimeError" not in turn.reply, turn.reply[-250:])

    print("\n2. The cart becomes unreadable later")
    shop2 = FakeShop(standard_catalogue())
    sid2, _s, turn2 = await start_mission(shop2, MSG)
    shop2.cart_fails = True
    t = await g.handle_command(adapter=shop2, command="next", message="next", state=turn2.state, cart_id="C1", memory_facts=None)
    check("'next' still answers and reports unknown, not a made-up remainder", t is not None and (t.budget_trace or {}).get("verdict") == "unknown"
          and (t.budget_trace or {}).get("remaining") is None, t and t.budget_trace)
    adv = await g.advance_from_cart(adapter=shop2, session_id=sid2, connection_id="conn_demo", cart_id="C1")
    check("advancing from an unreadable cart makes no claim and does not raise (returns None)", adv is None, adv)

    print("\n3. Search is down at the start")
    shop3 = FakeShop(standard_catalogue())
    shop3.search_fails = True
    sid3, spec3, turn3 = await start_mission(shop3, MSG)
    row = await store.get(sid3, "conn_demo")
    check("start returns None (ordinary handling takes over) and creates no mission state", turn3 is None and (row is None or not row.get("mission_items")),
          (turn3, row and row.get("mission_items")))

    print("\n4. A platform with no prices")
    cat = [product("S-A", "Alpha Running Shoes", None), product("K-1", "Crew Socks", None), product("E-1", "Energy Gel Box", None)]
    shop4 = FakeShop(cat)
    sid4, _s4, turn4 = await start_mission(shop4, MSG)
    tr4 = (turn4.budget_trace or {}) if turn4 else {}
    check("every stage unpriced: verdict unknown and all three stages are named", tr4.get("verdict") == "unknown" and len(tr4.get("unknown_stages", [])) == 3, tr4)
    check("no confident claim and no invented price", turn4 is not None and "Good so far" not in turn4.reply and "can't tell" in turn4.reply, turn4 and turn4.reply[-260:])
    check("no alternatives are invented without prices", tr4.get("alternatives_offered_ids") == [], tr4.get("alternatives_offered_ids"))

    print("\n5. A live product read fails while looking for alternatives")
    shop5 = FakeShop(standard_catalogue())
    sid5, _s5, turn5 = await start_mission(shop5, "I want shoes, socks and something for energy, total budget 4300")
    shop5.product_fails = {"S-A", "S-B", "S-C"}
    t5 = await g.handle_command(adapter=shop5, command="next", message="next", state=turn5.state, cart_id="C1", memory_facts=None)
    check("the turn still replies, offering nothing it could not fetch, with no error text",
          t5 is not None and (t5.budget_trace or {}).get("alternatives_offered_ids") == [] and "cannot fetch" not in t5.reply, t5 and t5.budget_trace)


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
