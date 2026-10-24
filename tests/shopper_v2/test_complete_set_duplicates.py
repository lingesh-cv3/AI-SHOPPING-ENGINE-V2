"""No duplicate suggestions, no duplicate announcements (AC-7).

A complement already in the cart is never offered; a product holds ONE role per reply (stage-fill, extra, alternative); two anchors that
declare the same complement yield ONE suggestion citing both; an extra announced after an add is not announced again after the next add,
and is repeated at most once in the closing summary (and only while it is still not in the cart).

    python tests/shopper_v2/test_complete_set_duplicates.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_duplicates.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_duplicates.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import start_mission  # noqa: E402
from _cs_fixtures import MSG, FakeShop, advance, cs_catalogue, ids  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. A complement already in the real cart is never offered")
    shop = FakeShop(cs_catalogue())
    shop.put_in_cart("X-SLIDE")  # the shopper already has the slide
    sid, _s, turn = await start_mission(shop, MSG)
    adv = await advance(shop, sid, "S-A")
    check("the slide is in the cart, so it is not offered as an extra", "X-SLIDE" not in ids(adv[1]) and "Optional extra" not in adv[0], (ids(adv[1]), adv[0][-250:]))
    check("...and the trace offered nothing", (g.COMPLETE_TRACE.get() or {}).get("extras_offered_ids") == [], g.COMPLETE_TRACE.get())

    print("\n2. An extra is announced once after an add, not again after the next add, and at most once in the summary")
    shop2 = FakeShop(cs_catalogue())
    sid2, _s2, _t2 = await start_mission(shop2, MSG)
    a1 = await advance(shop2, sid2, "S-A")
    check("after the shoe: the slide is announced as a card", "X-SLIDE" in ids(a1[1]) and "Optional extra" in a1[0], ids(a1[1]))
    a2 = await advance(shop2, sid2, "K-1")
    check("after the socks: it is NOT announced again", "X-SLIDE" not in ids(a2[1]) and "Optional extra" not in a2[0], (ids(a2[1]), a2[0][-250:]))
    a3 = await advance(shop2, sid2, "E-2")
    check("in the closing summary it is repeated ONCE as a last reminder (still not in the cart)", "X-SLIDE" in ids(a3[1]) and a3[0].count("Optional extra") == 1,
          (ids(a3[1]), a3[0][-400:]))
    from engine.db import shopping_state as store
    from engine.shopper_v2.state import MissionItem, ShoppingStateV2

    row = await store.get(sid2, "conn_demo")
    st = ShoppingStateV2(session_id=sid2, connection_id="conn_demo", mission_items=[MissionItem.model_validate(d) for d in row["mission_items"]])
    again = await g.handle_command(adapter=shop2, command="next", message="next", state=st, cart_id="C1", memory_facts=None)
    check("asking for the summary again does not repeat the extra a second time", "X-SLIDE" not in ids(again.products) and "Optional extra" not in again.reply,
          (ids(again.products), again.reply[-300:]))
    check("each announcement is remembered with the stages (bare id = after an add, ':summary' = closing)",
          any("X-SLIDE" in d.get("offered_complement_ids", []) and "X-SLIDE:summary" in d.get("offered_complement_ids", []) for d in row["mission_items"]),
          [d.get("offered_complement_ids") for d in row["mission_items"]])

    print("\n3. Two anchors declaring the same complement give ONE suggestion citing both")
    cat = cs_catalogue()
    next(p for p in cat if p.product_id == "K-1").compatible_with.append("X-SLIDE")  # the socks ALSO declare the slide
    shop3 = FakeShop(cat)
    sid3, _s3, _t3 = await start_mission(shop3, MSG)
    shop3.put_in_cart("S-A")
    shop3.put_in_cart("K-1")
    both = await g.advance_from_cart(adapter=shop3, session_id=sid3, connection_id="conn_demo", cart_id="C1")
    check("exactly one slide card", ids(both[1]).count("X-SLIDE") == 1, ids(both[1]))
    check("it cites BOTH anchors", "Alpha Running Shoes" in both[0] and "Crew Socks" in both[0].split("Optional extra")[-1], both[0][-420:])

    print("\n4. A product holds one role per reply")
    cat4 = cs_catalogue()
    next(p for p in cat4 if p.product_id == "S-A").compatible_with.append("E-2")  # E-2 is a stage-fill for the energy stage
    shop4 = FakeShop(cat4)
    sid4, _s4, _t4 = await start_mission(shop4, MSG)
    r4 = await advance(shop4, sid4, "S-A")
    tr = g.COMPLETE_TRACE.get() or {}
    check("the stage-fill item is not repeated as an extra, and appears once in the text", "E-2" not in tr.get("extras_offered_ids", []) and r4[0].count("Energy Chew Bar") == 1,
          (tr.get("extras_offered_ids"), r4[0].count("Energy Chew Bar")))

    print("\n5. Re-running the mission hook never repeats an announcement (after an add, or after the summary)")
    shop5 = FakeShop(cs_catalogue())
    sid5, _s5, _t5 = await start_mission(shop5, MSG)
    first = await advance(shop5, sid5, "S-A")
    check("precondition: the slide is announced once after the shoe", ids(first[1]).count("X-SLIDE") == 1 and first[0].count("Optional extra") == 1, ids(first[1]))
    check("the hook on an UNCHANGED cart produces nothing (no second announcement)",
          await g.advance_from_cart(adapter=shop5, session_id=sid5, connection_id="conn_demo", cart_id="C1") is None)
    await advance(shop5, sid5, "K-1")
    last = await advance(shop5, sid5, "E-1")
    check("the summary repeats it exactly once", ids(last[1]) == ["X-SLIDE"] and last[0].count("Optional extra") == 1, (ids(last[1]), last[0][-200:]))
    check("the hook after the summary produces nothing (no third announcement)",
          await g.advance_from_cart(adapter=shop5, session_id=sid5, connection_id="conn_demo", cart_id="C1") is None)


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
