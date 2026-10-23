"""The persisted measure and the trace (AC-15, AC-16).

`complement_offered` is recorded when an extra is shown; `complete_set_outcome` ONCE when the last stage closes (with or without a
budget); `events.complete_set_outcomes` returns `rate` None with no data and a hand-computed value for a known set of missions; and the
per-turn trace's offered ids equal the extra cards actually in the reply.

    python tests/shopper_v2/test_complete_set_measure.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_measure.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_measure.db"
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


async def event_rows(event_type, conn="conn_demo"):
    from sqlalchemy import select

    from engine.db import ShopperEvent
    from engine.db.session import session_scope

    async with session_scope() as db:
        return (await db.execute(select(ShopperEvent.session_id, ShopperEvent.detail).where(
            ShopperEvent.connection_id == conn, ShopperEvent.event_type == event_type))).all()


async def checks():
    from engine.execution import events
    from engine.shopper_v2 import guided_mission as g

    print("1. Zero data: an honest None, never a confident 0%")
    empty = await events.complete_set_outcomes("conn_demo")
    check("no finished mission -> finished 0 and rate None", empty["finished"] == 0 and empty["rate"] is None, empty)

    print("\n2. The trace equals what is on screen (AC-16)")
    shop = FakeShop(cs_catalogue())
    sid, _s, _t = await start_mission(shop, MSG)
    a = await advance(shop, sid, "S-A")
    tr = g.COMPLETE_TRACE.get() or {}
    stage_cards = {"K-1", "K-2"}
    extra_cards = [c for c in ids(a[1]) if c not in stage_cards]
    check("the trace's offered extras equal the extra cards in the reply", tr.get("extras_offered_ids") == extra_cards == ["X-SLIDE"], (tr.get("extras_offered_ids"), extra_cards))
    check("the trace is present on a mission with NO budget too", (await _no_budget_trace()) is not None)

    print("\n3. complement_offered is recorded when an extra is shown")
    off = await event_rows("complement_offered")
    mine = [r for r in off if r.session_id == sid]
    check("one event for the announcement after the add", len(mine) == 1 and mine[0].detail.get("where") == "after_add" and mine[0].detail.get("offered_ids") == ["X-SLIDE"],
          [(r.session_id, r.detail) for r in off])

    print("\n4. complete_set_outcome is recorded ONCE when the last stage closes")
    await advance(shop, sid, "K-1")
    check("not recorded while a stage is open", len(await event_rows("complete_set_outcome")) == 0)
    await advance(shop, sid, "E-2")
    out = await event_rows("complete_set_outcome")
    check("recorded once at the close, with the real cart's confirmed pairs (Alpha-Crew Socks is declared; Alpha-Slide is not in the cart)",
          len(out) == 1 and out[0].detail.get("confirmed_pairs_in_cart") == 1 and out[0].detail.get("stages_added") == 3, [r.detail for r in out])
    again = await g.advance_from_cart(adapter=shop, session_id=sid, connection_id="conn_demo", cart_id="C1")
    check("reading a finished mission again records nothing more", again is None and len(await event_rows("complete_set_outcome")) == 1)

    print("\n5. The aggregate matches a hand-computed value")
    shop_b = FakeShop(cs_catalogue())
    sid_b, _b, _bt = await start_mission(shop_b, MSG)
    await advance(shop_b, sid_b, "S-B", "K-2", "E-2")  # no declared pair among these three
    shop_c = FakeShop(cs_catalogue())
    sid_c, _c, _ct = await start_mission(shop_c, MSG)
    await advance(shop_c, sid_c, "S-A")
    shop_c.put_in_cart("X-SLIDE")  # the shopper takes the announced extra
    await advance(shop_c, sid_c, "K-1", "E-1")
    agg = await events.complete_set_outcomes("conn_demo")
    check("finished 3 missions; 2 held a merchant-declared pair; rate = 2/3", agg["finished"] == 3 and agg["with_confirmed_pair"] == 2 and abs(agg["rate"] - 2 / 3) < 1e-9, agg)
    check("extras_in_cart_at_close counts the slide the shopper took (1)", agg["extras_in_cart_at_close"] == 1, agg)
    check("extras_offered counts distinct (mission, add-on) pairs: A, C and the no-budget mission announced the slide; B (no declared complement outside its stages) none",
          agg["extras_offered"] == 3, agg)

    print("\n6. Another merchant sees none of it")
    other = await events.complete_set_outcomes("conn_kettle")
    check("rate None, nothing counted", other["finished"] == 0 and other["rate"] is None and other["extras_offered"] == 0, other)


async def _no_budget_trace():
    from engine.shopper_v2 import guided_mission as g

    s = FakeShop(cs_catalogue())
    sid, _sp, _t = await start_mission(s, "I want shoes, socks and something for energy")
    await advance(s, sid, "S-A")
    return g.COMPLETE_TRACE.get()


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
