"""Model-free handling of references like "I'll take this", "add these", "add both".

No provider (the model double is always unavailable), real graph, real Northfield
adapter, own sqlite file. When the target cannot be resolved SAFELY from what is
on screen the shopper gets a clear question - never "I couldn't process that
just now", never a guessed product, never a cart change. When it can be resolved
(one product in view, a counted pointer, an exact name) the result is a normal
add PROPOSAL for the shopper to approve.

    python tests/shopper_v2/test_step17_ambiguity_model_free.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_NAME = "_test_shopper_v2_step17.db"
_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", _DB_NAME)
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./{_DB_NAME}"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class DownClient:
    async def complete(self, **_):
        from engine.reasoning.llm import LLMUnavailable

        raise LLMUnavailable("test double: no model")


class Shopper:
    def __init__(self, engine, cid="conn_demo"):
        self.e, self.cid = engine, cid
        self.ad = engine.registry.adapter_for(cid)
        self.sid = f"s17a_{uuid.uuid4().hex[:10]}"
        self.cart_id = None

    async def start(self):
        self.cart_id = (await self.ad.create_cart()).cart_id
        return self

    async def say(self, msg):
        from engine.reasoning import ReasoningService
        from engine.shopper_v2.graph import run_turn_via_graph

        return await run_turn_via_graph(
            reasoning=ReasoningService(DownClient()), adapter=self.ad, session_id=self.sid,
            connection_id=self.cid, message=msg, cart_id=self.cart_id,
            decision=self.e.decision, gate=self.e.gate, execution=self.e.execution)

    async def cart(self):
        return await self.ad.get_cart(self.cart_id)

    async def pending(self):
        from engine.db import cart_proposals as store

        return await store.pending_for_cart(self.cid, self.cart_id, self.sid)


def bad_reply(r) -> bool:
    low = r.reply.lower()
    return "couldn't process" in low or "couldn't fully process" in low


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401

    await create_schema()
    engine = build_engine()
    try:
        await run(engine)
    finally:
        await engine.close()
        await dispose()


async def run(engine) -> None:
    print("1. Missing context")
    for msg in ("I'll take this", "add it", "buy this one"):
        s = await Shopper(engine).start()
        r = await s.say(msg)
        check(f"{msg!r} with nothing on screen asks what to add", "don't have a product in view" in r.reply.lower())
        check(f"{msg!r}: not a processing failure", not bad_reply(r))
        check(f"{msg!r}: no proposal, no cart change", await s.pending() == [] and (await s.cart()).is_empty)

    print("\n2. Ambiguous: several products on screen")
    s = await Shopper(engine).start()
    shown = await s.say("I need shoes")
    n_shown = len(shown.products)
    check("setup: several shoes shown", n_shown >= 3)
    for msg in ("I'll take this", "add it", "buy that one", "get this one"):
        r = await s.say(msg)
        check(f"{msg!r} asks which one and lists real options", "which one" in r.reply.lower() and "1." in r.reply)
        check(f"{msg!r}: not a processing failure", not bad_reply(r))
        check(f"{msg!r}: nothing proposed and nothing added", await s.pending() == [] and (await s.cart()).is_empty)
    r = await s.say("add these")
    check("'add these' with many on screen asks which, it does not add them all",
          "which one" in r.reply.lower() and await s.pending() == [] and (await s.cart()).is_empty)

    print("\n3. Unambiguous references")
    s = await Shopper(engine).start()
    await s.say("I need socks")
    r = await s.say("I'll take this")
    pend = await s.pending()
    check("ONE product on screen + 'I'll take this' -> a proposal for that product",
          len(pend) == 1 and "Socks" in r.reply and r.trace.get("capability") == "cart_action")
    check("the cart is not changed by the proposal", (await s.cart()).is_empty)

    s = await Shopper(engine).start()
    await s.say("show me running shoes under 5000")
    r = await s.say("add both")
    check("two on screen + 'add both' asks the first product's size (a per-item flow, no add)",
          "size" in r.reply.lower() and (await s.cart()).is_empty and await s.pending() == [])

    s = await Shopper(engine).start()
    await s.say("I need shoes")
    r = await s.say("add the third one")
    asked = (r.trace.get("fulfillment_pending") or {})
    # Step 25: an explicit add executes - a sized product first asks THAT product's size (no proposal, nothing added).
    check("'add the third one' resolves by position to exactly one product and asks its size",
          await s.pending() == [] and bool(asked.get("options")) and (await s.cart()).is_empty)
    check("...the third product that was shown", asked.get("product_id") == shown.products[2].product_id)
    r = await s.say("add the first two")
    check("'add the first two' starts the per-item flow (size question), still nothing added",
          "size" in r.reply.lower() and (await s.cart()).is_empty)

    s = await Shopper(engine).start()
    r = await s.say("add Trailblazer Running Shoe")
    asked = (r.trace.get("fulfillment_pending") or {})
    check("an exact name needs no context: it asks the Trailblazer's size (executes; no proposal, nothing added yet)",
          asked.get("product_id") == "P1001" and await s.pending() == [] and (await s.cart()).is_empty)

    print("\n4. 'add these' during a guided mission")
    s = await Shopper(engine).start()
    await s.say("I am going for a long run. I want shoes, socks and something for energy")
    from engine.db import shopping_state as store

    before = (await store.get(s.sid, s.cid))["mission_items"]
    for msg in ("add these to my cart", "add them", "add all", "put everything in my cart"):
        r = await s.say(msg)
        low = r.reply.lower()
        check(f"{msg!r}: a focused question about the OPEN stage, not a failure",
              "one stage at a time" in low and "running shoes" in low and not bad_reply(r))
        check(f"{msg!r}: no proposals created, nothing added", await s.pending() == [] and (await s.cart()).is_empty)
    after = (await store.get(s.sid, s.cid))["mission_items"]
    check("the mission plan and stage states are untouched",
          [(i["label"], i["stage"]) for i in before] == [(i["label"], i["stage"]) for i in after])
    r = await s.say("add the Trailblazer Running Shoe")
    asked = (r.trace.get("fulfillment_pending") or {})
    check("naming the product within the stage asks its size (the explicit command executes; nothing pending)",
          asked.get("product_id") == "P1001" and await s.pending() == [] and (await s.cart()).is_empty)

    print()
    print(f"  {failures} FAILURE(S)" if failures else "  all model-free ambiguity checks passed")


asyncio.run(main())
sys.exit(1 if failures else 0)
