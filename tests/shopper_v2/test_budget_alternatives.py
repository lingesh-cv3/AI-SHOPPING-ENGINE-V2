"""Budget-aware alternatives for the open stage (AC-6, AC-7).

When a stage's options would blow what is left after reserving the cheapest option of every OTHER stage, up to three cheaper,
IN-STOCK options from THAT stage's own verified candidates are offered as cards, each checked against a LIVE read. Nothing is
padded from outside the stage; nothing is added for the shopper (the fake platform raises on any write).

    python tests/shopper_v2/test_budget_alternatives.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_alternatives.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_alternatives.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import FakeShop, product, standard_catalogue, start_mission  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def ids(turn):
    return [p.product_id for p in (turn.products or [])]


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. A tight budget: cheaper in-stock options from THIS stage come back as cards")
    shop = FakeShop(standard_catalogue())
    sid, spec, turn = await start_mission(shop, "I want shoes, socks and something for energy, total budget 4300")
    tr = turn.budget_trace or {}
    check("verdict is tight (lows 3397 fit 4300, typicals 6997 do not)", tr.get("verdict") == "tight", tr.get("verdict"))
    check("left for shoes = 4300 - (499 + 899) = 2902, so only Alpha (1999) is offered",
          tr.get("alternatives_offered_ids") == ["S-A"], tr.get("alternatives_offered_ids"))
    check("the reply says what fits and names the cheaper option", "Within what's left for shoes" in turn.reply and "Alpha Running Shoes" in turn.reply, turn.reply[-320:])
    check("the offered product is a card (one tap from the existing Add)", "S-A" in ids(turn), ids(turn))
    check("nothing from another stage is offered as an alternative", all(p.startswith("S-") for p in tr.get("alternatives_offered_ids", [])),
          tr.get("alternatives_offered_ids"))
    check("live reads are bounded", shop.calls["get_product"] <= 40, dict(shop.calls))

    print("\n2. AC-7: only live, in-stock, fetchable products are offered")
    shop2 = FakeShop(standard_catalogue())
    sid2, _spec, turn2 = await start_mission(shop2, "I want shoes, socks and something for energy, total budget 4300")
    shop2.products["S-A"] = product("S-A", "Alpha Running Shoes", 1999, stock=False)  # sold out since the search
    t = await g.handle_command(adapter=shop2, command="next", message="next", state=turn2.state, cart_id="C1", memory_facts=None)
    check("a product that went out of stock since the search is not offered", "S-A" not in (t.budget_trace or {}).get("alternatives_offered_ids", ["S-A"]),
          t.budget_trace)
    check("...and the reply says plainly nothing fits (no padding)", "None of the in-stock shoes options I found fit within what's left" in t.reply, t.reply[-300:])
    shop3 = FakeShop(standard_catalogue())
    sid3, _s, turn3 = await start_mission(shop3, "I want shoes, socks and something for energy, total budget 4300")
    shop3.product_fails = {"S-A"}
    t3 = await g.handle_command(adapter=shop3, command="next", message="next", state=turn3.state, cart_id="C1", memory_facts=None)
    check("a product that cannot be fetched is never offered, and no error reaches the reply",
          "S-A" not in (t3.budget_trace or {}).get("alternatives_offered_ids", ["S-A"]) and "cannot fetch" not in t3.reply, t3.budget_trace)

    print("\n3. Nothing fits: said plainly, no extra products")
    shop4 = FakeShop(standard_catalogue())
    sid4, _s, turn4 = await start_mission(shop4, "I want shoes, socks and something for energy, total budget 3300")
    tr4 = turn4.budget_trace or {}
    check("verdict over (cheapest options total 3397 > 3300)", tr4.get("verdict") == "over", tr4.get("verdict"))
    check("no alternative within what is left (3300 - 1398 = 1902 < 1999)", tr4.get("alternatives_offered_ids") == [], tr4.get("alternatives_offered_ids"))
    check("the reply says so and shows no extra card", "None of the in-stock shoes options I found fit within what's left" in turn4.reply, turn4.reply[-320:])

    print("\n4. A comfortable budget needs no alternatives")
    shop5 = FakeShop(standard_catalogue())
    sid5, _s, turn5 = await start_mission(shop5, "I want shoes, socks and something for energy, total budget 20000")
    check("no alternatives are pushed when every option fits", (turn5.budget_trace or {}).get("alternatives_offered_ids") == [],
          (turn5.budget_trace or {}).get("alternatives_offered_ids"))
    check("and the verdict is fits", (turn5.budget_trace or {}).get("verdict") == "fits")


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
