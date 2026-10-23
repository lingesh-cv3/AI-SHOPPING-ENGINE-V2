"""The persisted measure behind the basket budget (value bar: "what number moves").

A budgeted guided mission records `basket_budget_set` when the shopper gives a total and `basket_budget_outcome` ONCE when the last open
stage closes, with the REAL cart's committed total. `events.basket_budget_outcomes` turns those into: budgeted missions, finished,
within budget, outcome-unknown, added-from-an-offered-alternative, and a rate that is None (never a confident 0%) with no data.

    python tests/shopper_v2/test_budget_measure.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_measure.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_measure.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import FakeShop, standard_catalogue, start_mission  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def finish(shop, sid, products):
    """Put each product in the REAL cart and advance, as the add path + the mission-advance hook do."""
    from engine.shopper_v2 import guided_mission as g

    out = None
    for pid in products:
        shop.put_in_cart(pid)
        out = await g.advance_from_cart(adapter=shop, session_id=sid, connection_id="conn_demo", cart_id="C1")
    return out


async def checks():
    from engine.execution import events

    print("1. Zero data: no budgeted mission yet - an honest None, not 0%")
    empty = await events.basket_budget_outcomes("conn_demo")
    check("nothing recorded: counts are 0 and the rate is None", empty["budgeted_missions"] == 0 and empty["finished"] == 0 and empty["rate"] is None, empty)

    print("\n2. A mission with no budget records nothing")
    shop0 = FakeShop(standard_catalogue())
    sid0, _s, _t = await start_mission(shop0, "I want shoes, socks and something for energy")
    await finish(shop0, sid0, ["S-B", "K-1", "E-2"])
    check("no budget stated -> no event at all", (await events.basket_budget_outcomes("conn_demo"))["budgeted_missions"] == 0)

    print("\n3. A mission that finishes under budget")
    shop1 = FakeShop(standard_catalogue())
    sid1, _s, _t = await start_mission(shop1, "I want shoes, socks and something for energy, total budget 8000")
    mid = await events.basket_budget_outcomes("conn_demo")
    check("starting with a budget is recorded as a budgeted mission, not yet finished", mid["budgeted_missions"] == 1 and mid["finished"] == 0 and mid["rate"] is None, mid)
    await finish(shop1, sid1, ["S-B", "K-1"])
    check("the outcome is NOT recorded while a stage is still open", (await events.basket_budget_outcomes("conn_demo"))["finished"] == 0)
    await finish(shop1, sid1, ["E-2"])
    one = await events.basket_budget_outcomes("conn_demo")
    check("when the last stage closes it is recorded once: 4999 + 499 + 899 = 6397 <= 8000", one["finished"] == 1 and one["within_budget"] == 1 and one["rate"] == 1.0, one)

    print("\n4. A second mission that ends over budget")
    shop2 = FakeShop(standard_catalogue())
    sid2, _s, _t = await start_mission(shop2, "I want shoes, socks and something for energy, total budget 3000")
    await finish(shop2, sid2, ["S-C", "K-2", "E-1"])
    two = await events.basket_budget_outcomes("conn_demo")
    check("finished 2, within 1, rate 0.5 (the cart really cost 8999 + 799 + 1799 > 3000)", two["finished"] == 2 and two["within_budget"] == 1 and two["rate"] == 0.5, two)

    print("\n5. Re-reading a finished mission does not double count")
    from engine.shopper_v2 import guided_mission as g

    again = await g.advance_from_cart(adapter=shop2, session_id=sid2, connection_id="conn_demo", cart_id="C1")
    check("advance on a finished mission records nothing more", again is None and (await events.basket_budget_outcomes("conn_demo"))["finished"] == 2)

    print("\n6. Adds that came from an offered cheaper alternative are counted")
    shop3 = FakeShop(standard_catalogue())
    sid3, _s, t3 = await start_mission(shop3, "I want shoes, socks and something for energy, total budget 4300")
    check("setup: the cheaper shoe was offered", (t3.budget_trace or {}).get("alternatives_offered_ids") == ["S-A"], t3.budget_trace)
    before3 = (await events.basket_budget_outcomes("conn_demo"))["added_from_alternatives"]
    await finish(shop3, sid3, ["S-A", "K-1", "E-2"])
    three = await events.basket_budget_outcomes("conn_demo")
    check("this mission adds exactly ONE to added_from_alternatives (the shoe taken from the offer; socks and energy were not offers)",
          three["added_from_alternatives"] == before3 + 1 and three["finished"] == 3, (before3, three))
    check("and mission 1 had also taken an offered cheaper shoe, so the running total is 2 (Bravo, then Alpha)", three["added_from_alternatives"] == 2, three)

    print("\n7. Skipping the last stage also closes the mission once")
    shop4 = FakeShop(standard_catalogue())
    sid4, _s, t4 = await start_mission(shop4, "I want shoes and socks, total budget 9000")
    await finish(shop4, sid4, ["S-B"])
    row = await (__import__("engine.db.shopping_state", fromlist=["x"]).get(sid4, "conn_demo"))
    from engine.shopper_v2.state import MissionItem, ShoppingStateV2

    state = ShoppingStateV2(session_id=sid4, connection_id="conn_demo", mission_items=[MissionItem.model_validate(d) for d in row["mission_items"]])
    before = (await events.basket_budget_outcomes("conn_demo"))["finished"]
    await g.handle_command(adapter=shop4, command="skip", message="skip socks", state=state, cart_id="C1", memory_facts=None)
    after = (await events.basket_budget_outcomes("conn_demo"))["finished"]
    check("skipping the last open stage records the outcome", after == before + 1, (before, after))

    print("\n8. Tenant isolation of the figure")
    other = await events.basket_budget_outcomes("conn_kettle")
    check("another merchant sees none of these missions and gets None, not a rate", other["budgeted_missions"] == 0 and other["finished"] == 0 and other["rate"] is None, other)


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
