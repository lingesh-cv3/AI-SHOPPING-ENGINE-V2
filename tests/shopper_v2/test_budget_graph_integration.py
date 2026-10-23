"""Basket budget through the REAL graph on BOTH demo merchants (AC-8, AC-14; platform independence).

No scripted model and no fake platform: `run_turn_via_graph` with the model unavailable, against the real Northfield (REST) and Kettle &
Bloom (GraphQL) merchants. The same engine code runs on both; nothing below branches on the platform.

  - the reply's budget figures equal `trace["basket_budget"]`, and `committed` equals the REAL cart's line totals read straight from the
    adapter;
  - a cheaper in-stock option is offered as a CARD and adding it goes through the existing add path (a sized product asks its size first;
    nothing is added by the budget code);
  - a total budget the cheapest options cannot meet is reported as "won't fit".

Needs Northfield (8001) and Kettle (8002) running.

    python tests/shopper_v2/test_budget_graph_integration.py
"""
from __future__ import annotations

import os
import sys
import uuid

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_graph.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_graph.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402
from decimal import Decimal  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


class Down:
    async def complete(self, **kw):
        from engine.reasoning.llm import LLMUnavailable

        raise LLMUnavailable("no model in this test")


class Shopper:
    def __init__(self, eng, cid):
        from engine.reasoning import ReasoningService

        self.eng, self.cid = eng, cid
        self.sid = f"bg_{uuid.uuid4().hex[:8]}"
        self.ad = eng.registry.adapter_for(cid)
        self.rs = ReasoningService(Down())
        self.cart_id = None

    async def start(self):
        self.cart_id = (await self.ad.create_cart()).cart_id
        return self

    async def say(self, message):
        from engine.shopper_v2.graph import run_turn_via_graph

        return await run_turn_via_graph(
            reasoning=self.rs, adapter=self.ad, session_id=self.sid, connection_id=self.cid, message=message, cart_id=self.cart_id,
            decision=self.eng.decision, gate=self.eng.gate, execution=self.eng.execution)

    async def real_line_total(self):
        cart = await self.ad.get_cart(self.cart_id)
        return sum((Decimal(str(ln.line_total.amount)) for ln in cart.lines), Decimal(0)), [ln.product_id for ln in cart.lines]


def bb(r):
    return (r.trace or {}).get("basket_budget") or {}


async def checks(eng):
    print("1. Northfield (REST): a budgeted mission through the real graph")
    s = await Shopper(eng, "conn_demo").start()
    r = await s.say("I want running shoes, socks and something for energy, total budget 6000")
    t = bb(r)
    check("the trace carries the basket budget", t.get("budget") == "6000" and t.get("currency") == "INR", t)
    check("the verdict is computed from real prices (cheapest 1999 + 499 + 1799 fit, typical total does not): tight", t.get("verdict") == "tight", t.get("verdict"))
    check("the reply states the same figures (nothing in the cart yet, 6000 available)", "all of your 6000 INR budget is available" in r.reply, r.reply[-300:])
    check("a cheaper in-stock option is offered as a CARD and in the trace", t.get("alternatives_offered_ids") == ["P1009"] and "P1009" in [p.product_id for p in r.products],
          (t.get("alternatives_offered_ids"), [p.product_id for p in r.products]))
    check("the reply names it and what is left for the stage", "Within what's left for running shoes (3702 INR)" in r.reply and "Starter Road Running Shoe" in r.reply, r.reply[-400:])
    check("nothing was added by starting the mission", (await s.real_line_total())[1] == [])

    r2 = await s.say("add Performance Socks Three Pack")
    real_total, real_ids = await s.real_line_total()
    t2 = bb(r2)
    check("the typed add used the existing path and the item is really in the cart", real_ids == ["P3002"], real_ids)
    check("trace 'committed' EQUALS the real cart's line total read from the adapter", t2.get("committed") is not None and Decimal(t2["committed"]) == real_total, (t2.get("committed"), real_total))
    check("trace 'remaining' = budget - real committed, and the reply says the same number",
          Decimal(t2["remaining"]) == Decimal(6000) - real_total and f"{(Decimal(6000) - real_total):.0f}" in r2.reply, (t2.get("remaining"), r2.reply[-300:]))
    check("the budget stays a clean 6000 (no 6000.0 float artifact)", t2.get("budget") == "6000", t2.get("budget"))

    r3 = await s.say("add Starter Road Running Shoe")
    _total3, ids3 = await s.real_line_total()
    check("an offered alternative is added through the SAME explicit-add path: a sized shoe asks its size first and nothing is added", "size" in r3.reply.lower() and ids3 == ["P3002"],
          (r3.reply[:200], ids3))

    # An open size question is answered BEFORE guided-mission commands (existing precedence), so resolve it first, as a shopper would.
    await s.say("skip")
    r4 = await s.say("total budget 2000")
    t4 = bb(r4)
    check("a total the cheapest options cannot meet (499 committed + 1799 energy + shoes > 2000) is 'over' and says so",
          t4.get("verdict") == "over" and "won't fit" in r4.reply, (t4.get("verdict"), r4.reply[-300:]))
    check("and offers no padding (nothing fits what is left)", t4.get("alternatives_offered_ids") == [], t4.get("alternatives_offered_ids"))

    print("\n2. Kettle & Bloom (GraphQL): the same engine code")
    k = await Shopper(eng, "conn_kettle").start()
    kr = await k.say("I want coffee, a grinder and a kettle, total budget 5000")
    kt = bb(kr)
    check("the same budget record appears for a different platform", kt.get("budget") == "5000" and kt.get("currency") == "INR" and kt.get("verdict") in ("fits", "tight", "over", "unknown"), kt)
    check("stages with nothing in stock are left out honestly, the priced stage is verified from real data", "Burr Hand Grinder" in kr.reply and kt.get("verdict") == "fits", kr.reply[-250:])
    kr2 = await k.say("add Burr Hand Grinder")
    kreal, kids = await k.real_line_total()
    check("the grinder is really in the cart and the summary states the budget outcome from line totals", kids == ["KB-EQP-10"] and "Against your 5000 INR budget the items come to" in kr2.reply, (kids, kr2.reply[-350:]))
    check("it does not hide that delivery and tax change the total", "With delivery and tax the cart total is" in kr2.reply and "over your budget" in kr2.reply, kr2.reply[-300:])


async def main():
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401 - registers the V2 tables before create_schema

    await create_schema()
    eng = build_engine()
    try:
        await checks(eng)
    finally:
        await eng.close()
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
