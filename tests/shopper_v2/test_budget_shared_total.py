"""ONE shared total budget across a guided mission (AC-1, AC-2, AC-3).

Before this feature a budget was per stage only: "...energy under 5000" produced a stage called "energy under", "total budget 8000"
became a stage called "total budget", the number was never stored for the basket, and nothing said what was left. Model-free, in
process, against a controllable fake platform (FakeShop); every figure asserted comes from the fake's REAL cart.

    python tests/shopper_v2/test_budget_shared_total.py
"""
from __future__ import annotations

import os
import sys
import uuid

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_shared_total.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_shared_total.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402
from decimal import Decimal  # noqa: E402

from _budget_fixtures import FakeShop, standard_catalogue, stored_total  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def new_mission(shop, message="I want shoes, socks and something for energy, total budget 8000"):
    from engine.db import shopping_state as store
    from engine.shopper_v2 import guided_mission as g
    from engine.shopper_v2.state import ShoppingStateV2

    sid = f"bud_{uuid.uuid4().hex[:8]}"
    state = ShoppingStateV2(session_id=sid, connection_id="conn_demo")
    spec = g.parse_mission(message)
    turn = await g.start(adapter=shop, spec=spec, message=message, state=state, cart_id="C1", memory_facts=None) if spec else None
    return sid, spec, turn, store


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. A total budget in the mission message is parsed once and stored")
    shop = FakeShop(standard_catalogue())
    sid, spec, turn, store = await new_mission(shop)
    check("the message is a mission with exactly three stages (the clause is not a stage)",
          spec is not None and [s.label for s in spec.stages] == ["shoes", "socks", "energy"], spec and [s.label for s in spec.stages])
    check("the total budget is read from the message", spec is not None and spec.total_budget == Decimal(8000), spec and spec.total_budget)
    check("the mission starts", turn is not None)
    row = await store.get(sid, "conn_demo")
    check("the shared total is stored on the mission's guided stages (additive JSON, no migration)", stored_total(row) == 8000,
          row and stored_total(row))
    check("the session-wide budget_amount column is NOT written (other code reads it as a per-product cap)", (row or {}).get("budget_amount") in (None, ""),
          (row or {}).get("budget_amount"))
    check("the reply states a verdict from real prices (fits: lows 3397, typical 6997 <= 8000)",
          turn is not None and "8000" in turn.reply and "Good so far" in turn.reply, turn and turn.reply[-260:])
    check("the trace record carries the figures", turn is not None and turn.budget_trace and turn.budget_trace["verdict"] == "fits"
          and turn.budget_trace["remaining"] in ("8000", "8000.00", "8000.0"), turn and turn.budget_trace)

    print("\n2. Phrasings that used to corrupt the stages")
    for msg, want in (("I want shoes, socks and something for energy under 5000", 5000),
                      ("I need shoes, socks and gels, total budget 8000", 8000),
                      ("I want shoes, socks and gels for 8000 rupees in all", 8000)):
        s = g.parse_mission(msg)
        check(f"{msg!r}: three clean stages and the amount", s is not None and len(s.stages) == 3 and all(
            "budget" not in x.label and "under" not in x.label for x in s.stages) and s.total_budget == want,
              s and ([x.label for x in s.stages], s.total_budget))
    mid = g.parse_mission("I want shoes under 5000 and socks")
    check("a mid-sentence per-item cap is NOT turned into the basket budget", mid is None or mid.total_budget is None, mid and mid.total_budget)

    print("\n3. Change, clear and stage-cap commands")
    state = turn.state
    check("'total budget 6000' is a total-budget command", g.classify_command("total budget 6000", state) == "total_budget",
          g.classify_command("total budget 6000", state))
    t2 = await g.handle_command(adapter=shop, command="total_budget", message="total budget 6000", state=state, cart_id="C1", memory_facts=None)
    row = await store.get(sid, "conn_demo")
    check("a later total replaces the stored one", t2 is not None and stored_total(row) == 6000,
          row and stored_total(row))
    check("'keep the socks under 600' stays a per-stage cap",
          g.classify_command("keep the socks under 600", t2.state) == "budget", g.classify_command("keep the socks under 600", t2.state))
    t3 = await g.handle_command(adapter=shop, command="budget", message="keep the socks under 600", state=t2.state, cart_id="C1", memory_facts=None)
    row = await store.get(sid, "conn_demo")
    socks = next(i for i in t3.state.mission_items if i.label == "socks")
    check("the stage cap narrowed only the socks stage", socks.candidates == ["K-1"], socks.candidates)
    check("...and left the shared amount unchanged", stored_total(row) == 6000, row and stored_total(row))
    check("'no budget' is a clear command", g.classify_command("no budget", t3.state) == "clear_budget")
    t4 = await g.handle_command(adapter=shop, command="clear_budget", message="no budget", state=t3.state, cart_id="C1", memory_facts=None)
    row = await store.get(sid, "conn_demo")
    check("clearing removes the stored budget", t4 is not None and stored_total(row) is None, row and stored_total(row))
    check("a reply after clearing says nothing about budget fit", "Good so far" not in t4.reply and "won't fit" not in t4.reply, t4.reply[:200])

    print("\n4. What is left is computed from the REAL cart (AC-3)")
    shop = FakeShop(standard_catalogue())
    sid, spec, turn, store = await new_mission(shop)
    shop.put_in_cart("S-B")  # Bravo Running Shoes, 4999
    adv = await g.advance_from_cart(adapter=shop, session_id=sid, connection_id="conn_demo", cart_id="C1")
    check("an add advances the mission", adv is not None)
    text = adv[0] if adv else ""
    check("the next reply states what is left from the cart (8000 - 4999 = 3001)", "3001" in text, text[:300])
    # a failed / approval-pending add leaves the cart unchanged -> nothing is spent
    shop2 = FakeShop(standard_catalogue())
    sid2, _s, turn2, _st = await new_mission(shop2)
    t = await g.handle_command(adapter=shop2, command="next", message="next", state=turn2.state, cart_id="C1", memory_facts=None)
    check("with nothing really in the cart the full budget is still left (a failed add reduces nothing)",
          "8000" in t.reply and t.budget_trace and t.budget_trace["committed"] in ("0", "0.00"), t.budget_trace)

    print("\n5. Over budget after an add is said plainly")
    shop3 = FakeShop(standard_catalogue())
    sid3, _s, turn3, _st = await new_mission(shop3, "I want shoes, socks and something for energy, total budget 3000")
    check("a budget the cheapest options cannot meet is reported as not fitting from the start (3397 > 3000)",
          turn3 is not None and "won't fit" in turn3.reply and turn3.budget_trace["verdict"] == "over", turn3 and turn3.reply[-300:])
    shop3.put_in_cart("S-C")  # 8999 > 3000
    adv3 = await g.advance_from_cart(adapter=shop3, session_id=sid3, connection_id="conn_demo", cart_id="C1")
    check("after an over-budget add the reply says by how much", adv3 is not None and "over your budget" in adv3[0] and "5999" in adv3[0],
          adv3 and adv3[0][:300])



    print("\n6. A per-search budget from ordinary discovery is NOT a basket total (invariant-review finding)")
    from engine.shopper_v2 import basket_budget as bb
    from engine.shopper_v2.state import ShoppingStateV2

    shop6 = FakeShop(standard_catalogue())
    sid6 = f"bud_{uuid.uuid4().hex[:8]}"
    # what the discovery/understanding path leaves in the session-wide column after "show me shoes under 3000"
    discovery_state = ShoppingStateV2(session_id=sid6, connection_id="conn_demo", budget_amount="3000", budget_currency="INR")
    msg6 = "I want shoes, socks and something for energy"
    t6 = await g.start(adapter=shop6, spec=g.parse_mission(msg6), message=msg6, state=discovery_state, cart_id="C1", memory_facts=None)
    check("the mission does NOT adopt that number as its total", t6 is not None and "budget" not in t6.reply.lower() and t6.budget_trace is None,
          t6 and t6.reply[-220:])
    row6 = await store.get(sid6, "conn_demo")
    check("and nothing was written to the session-wide budget column by the mission", (row6 or {}).get("budget_amount") in (None, ""), (row6 or {}).get("budget_amount"))

    print("\n7. Small numbers are quantities, not budgets")
    check("'I need 2 in all' is not a budget", bb.parse_budget("I need 2 in all") is None, bb.parse_budget("I need 2 in all"))
    check("'3 total' is not a total budget phrase", bb.is_total_phrase("3 total") is False)
    check("a real small-currency total with a symbol still counts", bb.parse_budget("budget of Rs 5 in all") == Decimal(5), bb.parse_budget("budget of Rs 5 in all"))
    check("a normal budget still parses", bb.parse_budget("total budget 8000") == Decimal(8000))


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
