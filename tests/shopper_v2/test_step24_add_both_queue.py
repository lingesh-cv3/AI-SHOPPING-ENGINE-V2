"""'add both' after a comparison queues EXACTLY the compared products - and nothing from another topic.

Step 24 triage of conversational-flow-review case 8. The browser spec asserted that the first reply to "add
both to the cart" names both products. Since the Step 17 add contract an add is a proposal the shopper
approves (and a sized product asks for its size first), so "add both" works through the products one at a
time and the first reply is only the first product's guided step. What the spec's title actually requires -
real, separate adds for exactly the compared products - is a property of the fulfillment queue, asserted here
at the state level (the browser spec now asserts the user-visible first step).

The model is unavailable for every turn (deterministic path).

    python tests/shopper_v2/test_step24_add_both_queue.py   (needs Northfield on 8001, Kettle on 8002)
"""
from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_step24_add_both.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_step24_add_both.db"
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
    def __init__(self, eng):
        from engine.reasoning import ReasoningService

        self.eng = eng
        self.sid = f"s24b_{uuid.uuid4().hex[:8]}"
        self.ad = eng.registry.adapter_for("conn_demo")
        self.rs = ReasoningService(Down())
        self.cart_id = None

    async def start(self):
        self.cart_id = (await self.ad.create_cart()).cart_id
        return self

    async def say(self, message):
        from engine.shopper_v2.graph import run_turn_via_graph

        return await run_turn_via_graph(
            reasoning=self.rs, adapter=self.ad, session_id=self.sid, connection_id="conn_demo", message=message,
            cart_id=self.cart_id, decision=self.eng.decision, gate=self.eng.gate, execution=self.eng.execution)

    async def queue(self):
        from engine.shopper_v2.state import load

        return list((await load(self.sid, "conn_demo")).fulfillment_queue)

    async def cart_ids(self):
        return [l.product_id for l in (await self.ad.get_cart(self.cart_id)).lines]


async def scenarios(eng):
    print("1. Two compared products: both are queued, in order, and nothing is added yet")
    s = await Shopper(eng).start()
    r1 = await s.say("show me running shoes under 5000")
    pair = [p.product_id for p in r1.products]
    await s.say("compare both")
    r3 = await s.say("add both to the cart")
    q = await s.queue()
    check("setup: exactly two products were compared", len(pair) == 2, pair)
    check("the queue is exactly the compared pair, in order", [i.product_id for i in q] == pair, [i.product_id for i in q])
    check("the first reply is the first product's guided step (size question or proposal)",
          any(w in r3.reply.lower() for w in ("size", "confirm", "proposal")), r3.reply[:160])
    check("nothing has been added to the cart (the shopper's own choices come next)", await s.cart_ids() == [])

    print("\n2. A different topic in view: 'add both' never mixes in products from the earlier topic")
    s = await Shopper(eng).start()
    r1 = await s.say("show me running shoes under 5000")
    shoes = {p.product_id for p in r1.products}
    await s.say("compare both")
    r3 = await s.say("show me socks")
    socks = {p.product_id for p in r3.products}
    await s.say("add both to the cart")
    q = {i.product_id for i in await s.queue()}
    check("setup: socks are in view and are not the shoes", len(socks) == 1 and not (socks & shoes), (socks, shoes))
    check("the queue never pairs the socks with the earlier shoes", not (q & socks and q & shoes), q)

    print("\n3. One product in view: no second product is invented")
    s = await Shopper(eng).start()
    r1 = await s.say("show me socks")
    await s.say("add both to the cart")
    q = await s.queue()
    check("at most the one product in view", len(q) <= 1 and all(i.product_id in {p.product_id for p in r1.products} for i in q),
          [i.product_id for i in q])


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
