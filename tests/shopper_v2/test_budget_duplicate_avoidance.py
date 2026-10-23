"""Duplicate avoidance across stages (AC-10).

A product that is a candidate of two stages is offered in ONE (the first that has it, and a stage is never emptied by this); a
product already in the REAL cart is never offered again in a later stage; one cart line completes at most one stage. Adding itself
stays refused at its existing source (`fulfillment.start_fulfillment`, untouched) - the fake platform raises on any write.

    python tests/shopper_v2/test_budget_duplicate_avoidance.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_duplicates.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_duplicates.db"
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


def stage(turn_state, label):
    return next(i for i in turn_state.mission_items if i.label == label)


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. A product that is a candidate of two stages is offered in one")
    cat = standard_catalogue() + [product("K-3", "Running Socks Pro", 799), product("K-4", "Wool Socks", 650)]
    shop = FakeShop(cat)
    sid, spec, turn = await start_mission(shop, "I want running socks, socks and shoes, total budget 9000")
    running, socks = stage(turn.state, "running socks"), stage(turn.state, "socks")
    check("'Running Socks Pro' is a candidate of the first stage that has it", "K-3" in running.candidates, running.candidates)
    check("...and is NOT repeated in the second stage (it still has its own options)", "K-3" not in socks.candidates and socks.candidates == ["K-1", "K-2", "K-4"],
          socks.candidates)
    check("the trace counts what was filtered", (turn.budget_trace or {}).get("duplicates_filtered") == 1, turn.budget_trace)
    check("prices stay aligned to the remaining candidates", [str(p) for p in socks.candidate_prices] == ["499", "799", "650"], socks.candidate_prices)

    print("\n2. A product in the REAL cart is never offered again, and one line completes one stage")
    shop2 = FakeShop(standard_catalogue())
    sid2, _s, turn2 = await start_mission(shop2, "I want shoes, running shoes and socks, total budget 12000")
    shoes2, running2 = stage(turn2.state, "shoes"), stage(turn2.state, "running shoes")
    check("setup: both shoe stages list the same three candidates (a stage is never emptied)", shoes2.candidates == running2.candidates == ["S-A", "S-B", "S-C"],
          (shoes2.candidates, running2.candidates))
    shop2.put_in_cart("S-A")
    adv = await g.advance_from_cart(adapter=shop2, session_id=sid2, connection_id="conn_demo", cart_id="C1")
    from engine.db import shopping_state as store

    row = await store.get(sid2, "conn_demo")
    stages = {d["label"]: d["stage"] for d in row["mission_items"]}
    check("the cart line completed ONE stage only", stages["shoes"] == "added" and stages["running shoes"] == "pending", stages)
    shown = [p.product_id for p in (adv[1] if adv else [])]
    check("the next stage's cards exclude what is already in the cart", adv is not None and "S-A" not in shown and shown == ["S-B", "S-C"], shown)

    print("\n3. Everything that fit is already taken: said plainly, not silently dropped")
    only = FakeShop([product("S-A", "Alpha Running Shoes", 1999), product("K-1", "Crew Socks", 499)])
    sid3, _s3, turn3 = await start_mission(only, "I want shoes, running shoes and socks, total budget 9000")
    only.put_in_cart("S-A")
    adv3 = await g.advance_from_cart(adapter=only, session_id=sid3, connection_id="conn_demo", cart_id="C1")
    check("the stage whose only candidate is in the cart says so", adv3 is not None and "already in your cart or chosen for another stage" in adv3[0],
          adv3 and adv3[0][:300])
    check("...and shows no cards for it", adv3 is not None and adv3[1] == [], adv3 and [p.product_id for p in adv3[1]])


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
