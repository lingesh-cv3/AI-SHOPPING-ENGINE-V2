"""A cart-budget question gets a CONCRETE reduction suggestion - from the budget the shopper stated.

Found in Step 24 triage of the conversational-flow-review browser failures. "My cart is over 10000. What can
I change to bring it under budget?" with a 24999 INR GPS watch in the cart was answered with an unrelated
finding ("here's what I did notice: ... goes with heart rate chest strap"). Root cause: the handler
(`cart_intelligence_v2.py`, deterministic, no model involved) only extracted a target from "under/below/
less than N" phrasing, so "over 10000 ... under budget" produced no target and the concrete-reduction path
(which exists and is correct) never ran; the docstring's "or, failing that, message-stated budget" fallback
to the budget the shopper stated earlier in the conversation was also never implemented.

No provider is involved: the model is unavailable for every turn here (the handler never calls one).

    python tests/shopper_v2/test_step24_cart_budget_reduction.py   (needs Northfield on 8001, Kettle on 8002)
"""
from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_step24_cart_budget.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_step24_cart_budget.db"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool, got: object = None) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


class Down:
    async def complete(self, **kw):
        from engine.reasoning.llm import LLMUnavailable

        raise LLMUnavailable("no model in this test")


class Shopper:
    def __init__(self, eng, cid="conn_demo"):
        from engine.reasoning import ReasoningService

        self.eng, self.cid = eng, cid
        self.sid = f"s24_{uuid.uuid4().hex[:8]}"
        self.ad = eng.registry.adapter_for(cid)
        self.rs = ReasoningService(Down())
        self.cart_id = None

    async def start(self):
        self.cart_id = (await self.ad.create_cart()).cart_id
        return self

    async def put(self, product_id, qty=1, variant_id=None):
        await self.ad.add_to_cart(self.cart_id, product_id, variant_id=variant_id, quantity=qty)

    async def say(self, message):
        from engine.shopper_v2.graph import run_turn_via_graph

        return await run_turn_via_graph(
            reasoning=self.rs, adapter=self.ad, session_id=self.sid, connection_id=self.cid, message=message,
            cart_id=self.cart_id, decision=self.eng.decision, gate=self.eng.gate, execution=self.eng.execution)


async def product_id(eng, title):
    ad = eng.registry.adapter_for("conn_demo")
    r = await ad.search_products(title, limit=5)
    return next(p.product_id for p in list(getattr(r, "products", r)) if p.title == title)


async def scenarios(eng):
    gps = await product_id(eng, "GPS Watch Series 4")
    socks = await product_id(eng, "Performance Socks Three Pack")
    print("1. The exact browser-spec case: one 24999 INR item, 'over 10000 ... bring it under budget'")
    s = await Shopper(eng).start()
    await s.put(gps)
    r = await s.say("My cart is over 10000. What can I change to bring it under budget?")
    check("a concrete removal is suggested, naming the real line and the 10000 target",
          "Removing GPS Watch Series 4" in r.reply and "10000" in r.reply, r.reply[:200])
    check("not the unrelated 'here's what I did notice' finding", "did notice" not in r.reply and "chest strap" not in r.reply, r.reply[:200])

    print("\n2. Several lines: the minimum number of removals, largest first")
    s = await Shopper(eng).start()
    await s.put(gps)
    await s.put(socks)
    r = await s.say("My cart is over 10000. What can I change to bring it under budget?")
    check("only the GPS watch needs to go (the socks stay)", "Removing GPS Watch Series 4" in r.reply and "Socks" not in r.reply, r.reply[:240])

    print("\n3. Phrasings that already worked still do")
    s = await Shopper(eng).start()
    await s.put(gps)
    r = await s.say("Bring it under 10000")
    check("'bring it under 10000'", "Removing GPS Watch Series 4" in r.reply, r.reply[:160])

    print("\n4. The budget the shopper stated earlier in the conversation is the target when the message names none")
    s = await Shopper(eng).start()
    await s.say("show me running shoes under 5000")
    await s.put(gps)
    r = await s.say("this is too expensive, how can I bring it down?")
    check("target = the stated 5000 budget", "Removing GPS Watch Series 4" in r.reply and "5000" in r.reply, r.reply[:200])

    print("\n5. Honesty: no invented target, no false fix")
    s = await Shopper(eng).start()
    await s.put(socks)
    r = await s.say("My cart is over 10000. What can I change to bring it under budget?")
    check("a cart already under the number is not 'fixed' by removing things", "Removing" not in r.reply, r.reply[:200])
    s = await Shopper(eng).start()
    await s.put(gps)
    r = await s.say("what can I change to bring it under budget?")
    check("no number in the message and no stated budget: no made-up target", "Removing" not in r.reply and "Reducing" not in r.reply, r.reply[:200])

    print("\n6. Empty cart / unrelated: unchanged")
    s = await Shopper(eng).start()
    r = await s.say("My cart is over 10000. What can I change to bring it under budget?")
    check("an empty cart gets no removal advice", "Removing" not in r.reply, r.reply[:160])


async def main():
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401 - registers the V2 tables before create_schema

    await create_schema()
    eng = build_engine()
    try:
        await scenarios(eng)
    finally:
        await eng.close()
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
