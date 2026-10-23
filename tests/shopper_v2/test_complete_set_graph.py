"""Cross-stage compatibility and complete-the-set through the REAL graph on BOTH demo merchants (AC-12, AC-16, platform independence).

No scripted model and no fake platform: `run_turn_via_graph` with the model unavailable, against the real Northfield (REST) and Kettle &
Bloom (GraphQL) merchants and their seeded `compatible_with` declarations. The same engine code serves both; nothing here branches on
the platform. The shopper's own typed add reaches the existing add path (a sized add-on asks its size first, a repeat add is refused).
Checkout and payment are never touched.

Needs Northfield (8001) and Kettle (8002) running.

    python tests/shopper_v2/test_complete_set_graph.py
"""
from __future__ import annotations

import os
import sys
import uuid

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_graph.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_graph.db"
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
        self.sid = f"cs_{uuid.uuid4().hex[:8]}"
        self.ad = eng.registry.adapter_for(cid)
        self.rs = ReasoningService(Down())
        self.cart_id = None
        self.last = None

    async def start(self):
        self.cart_id = (await self.ad.create_cart()).cart_id
        return self

    async def say(self, message, variant=None):
        from engine.shopper_v2.graph import run_turn_via_graph

        self.last = await run_turn_via_graph(
            reasoning=self.rs, adapter=self.ad, session_id=self.sid, connection_id=self.cid, message=message, cart_id=self.cart_id,
            decision=self.eng.decision, gate=self.eng.gate, execution=self.eng.execution, selected_variant_id=variant)
        return self.last

    def size_option(self):
        p = (self.last.trace or {}).get("fulfillment_pending") or {}
        opts = p.get("options") or []
        return opts[0]["variant_id"] if opts else None

    async def cart(self):
        c = await self.ad.get_cart(self.cart_id)
        return sum((Decimal(str(ln.line_total.amount)) for ln in c.lines), Decimal(0)), [ln.product_id for ln in c.lines]


def cs(r):
    return (r.trace or {}).get("complete_set") or {}


def ids(r):
    return [p.product_id for p in (r.products or [])]


async def checks(eng):
    from engine.execution import events

    print("1. Northfield (REST): the whole guided journey through the real graph")
    s = await Shopper(eng, "conn_demo").start()
    r = await s.say("I want shoes, socks and something for energy, total budget 9000")
    check("the trace carries the complete-set record at the first prompt, even with an empty cart", r.trace and "complete_set" in r.trace and cs(r).get("reason") == "no_anchors", cs(r))
    check("a declared pair across stages is noted at the first prompt (Trailblazer -> Performance Socks)",
          "Trailblazer Running Shoe is also listed by the merchant as going with Performance Socks Three Pack" in r.reply
          and cs(r).get("forward_pairs") == [{"option": "P1001", "later": "P3002", "stage": "socks"}], (cs(r).get("forward_pairs"), r.reply[-300:]))

    r2 = await s.say("add Trailblazer Running Shoe")
    check("the add goes through the existing path: a sized shoe asks its size first and nothing is added", "size" in r2.reply.lower() and (await s.cart())[1] == [], r2.reply[:160])
    r3 = await s.say("8", variant=s.size_option())
    line_total, in_cart = await s.cart()
    rem = Decimal(9000) - line_total
    check("the shoe is really in the cart", in_cart == ["P1001"], in_cart)
    check("the socks stage names the merchant-declared pair with the shoe actually in the cart",
          "The merchant lists Performance Socks Three Pack as going with your Trailblazer Running Shoe" in r3.reply and cs(r3).get("labels") == [{"option": "P3002", "anchor": "P1001"}],
          (cs(r3).get("labels"), r3.reply[-420:]))
    check("the declared add-on is an optional extra CARD, with price and budget effect computed from the real cart",
          "P1006" in ids(r3) and "Optional extra, not on your list - the merchant lists Recovery Slide (1499 INR)" in r3.reply
          and f"from {rem:.0f} INR to {(rem - 1499):.0f} INR" in r3.reply, (ids(r3), rem, r3.reply[-420:]))
    check("the trace's offered extras equal the extra card", cs(r3).get("extras_offered_ids") == ["P1006"], cs(r3).get("extras_offered_ids"))

    r4 = await s.say("add Recovery Slide")
    check("typing 'add' on the extra reaches the same add path: a sized add-on asks its size first, nothing added yet", "size" in r4.reply.lower() and (await s.cart())[1] == ["P1001"], r4.reply[:160])
    await s.say("S", variant=s.size_option())
    check("after the shopper chooses a size the add-on is really in the cart", (await s.cart())[1] == ["P1001", "P1006"], await s.cart())
    r5 = await s.say("add Recovery Slide")
    check("a repeat add is refused by the existing path", "already have that in your cart" in r5.reply, r5.reply[:160])

    r6 = await s.say("add Performance Socks Three Pack")
    check("after the next add the slide (now in the cart) is not announced again", "Optional extra" not in r6.reply and "P1006" not in ids(r6), r6.reply[-300:])
    r7 = await s.say("add Energy Gel Box of 12")
    check("the closing summary names the confirmed pairs among the real cart (shoe with socks and with slide)",
          "That completes your list" in r7.reply and "Performance Socks Three Pack as going with your Trailblazer Running Shoe" in r7.reply
          and "Recovery Slide as going with your Trailblazer Running Shoe" in r7.reply, r7.reply[-520:])
    check("it offers no extra the shopper already has, and the trace is present on the final turn", "Optional extra" not in r7.reply and "complete_set" in (r7.trace or {}), cs(r7))
    check("no word about incompatibility anywhere in the journey", not any(w in (x.reply.lower()) for x in (r, r2, r3, r4, r5, r6, r7)
                                                                          for w in ("incompatible", "not compatible", "won't work", "doesn't fit")))

    agg = await events.complete_set_outcomes("conn_demo")
    check("the persisted measure recorded the finished mission: 2 declared pairs in the real cart and the extra taken", agg["finished"] == 1 and agg["with_confirmed_pair"] == 1
          and agg["extras_in_cart_at_close"] == 1 and agg["rate"] == 1.0, agg)

    print("\n2. Kettle & Bloom (GraphQL): the same engine code")
    k = await Shopper(eng, "conn_kettle").start()
    kr = await k.say("I want a grinder, a dripper and a scale, total budget 12000")
    check("every card is Kettle's own (no Northfield product leaks in)", all(p.startswith("KB-") for p in ids(kr)), ids(kr))
    kr2 = await k.say("add Burr Hand Grinder")
    check("the grinder is really in the cart", (await k.cart())[1] == ["KB-EQP-10"], await k.cart())
    check("the merchant's declared add-on is offered as a card with its budget effect, citing the grinder",
          "KB-BLD-07" in ids(kr2) and "Optional extra, not on your list - the merchant lists House Filter Blend (1050 INR) as going with your Burr Hand Grinder" in kr2.reply, kr2.reply[-520:])
    check("a declared pair between the dripper and the scale (two open stages) is noted", "Pour Over Dripper is also listed by the merchant as going with Digital Brew Scale" in kr2.reply, kr2.reply[-520:])
    check("nothing from the other merchant, and no incompatibility wording", "Recovery Slide" not in kr2.reply and "Trailblazer" not in kr2.reply and "incompatible" not in kr2.reply.lower())
    other = await events.complete_set_outcomes("conn_kettle")
    check("Kettle's measure is its own (the mission is not finished yet, so nothing is counted)", other["finished"] == 0 and other["rate"] is None, other)


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
