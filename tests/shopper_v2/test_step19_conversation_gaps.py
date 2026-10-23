"""Step 19: the deterministic conversation gaps, model-free, in-process.

  1. Generic add requests ("add something blue", "add that one", "buy running
     shoes") that cannot be tied to a product SAFELY are answered with a focused
     question - never an ordinary search result list, never a guess - and create
     neither a proposal nor a cart change. On-screen context resolves them
     deterministically where it is unambiguous.
  2. A STALE proposal is not approved by a typed "yes" (the accept core expires it);
     a second "yes" then has nothing to approve.
  3. Kettle & Bloom coffee intent: "I need a fruity coffee", "chocolatey coffee",
     "decaf coffee", an unavailable attribute, a sold-out match - answered from the
     merchant's OWN declared coffee range (departments Single origin / Blends),
     descriptors matched only against what each product's data says, in-stock
     options returned, no equipment/gifts/subscriptions, and never a claim that the
     shop does not sell coffee. Northfield does not start offering coffee.

No provider (the model double is always unavailable), real graph, real adapters
(Northfield 8001, Kettle 8002), own sqlite file set BEFORE any engine import.

    python tests/shopper_v2/test_step19_conversation_gaps.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_NAME = "_test_shopper_v2_step19.db"
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
        self.sid = f"s19_{uuid.uuid4().hex[:10]}"
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


def titles(r):
    return [p.title for p in r.products]


def bad(r) -> bool:
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
    print("1. Generic add requests")
    for msg in ("add something blue", "buy something nice", "add anything", "add a thing for me"):
        s = await Shopper(engine).start()
        r = await s.say(msg)
        check(f"{msg!r}: asks for the product, not a search result or a failure",
              r.trace.get("capability") == "cart_action_ambiguous" and "not sure which product" in r.reply and not bad(r))
        check(f"{msg!r}: no products listed, no proposal, no cart change",
              r.products == [] and await s.pending() == [] and (await s.cart()).is_empty)
    s = await Shopper(engine).start()
    r = await s.say("add that one")
    check("'add that one' with nothing on screen asks what to add", "don't have a product in view" in r.reply and await s.pending() == [])
    s = await Shopper(engine).start()
    r = await s.say("add running shoes to my cart")
    check("a category phrase lists REAL matching products and asks which (no proposal yet)",
          "Which one would you like me to add?" in r.reply and len(r.products) >= 2
          and all("Shoe" in t or "Runner" in t for t in titles(r)))
    check("...no proposal and no cart change before the product is identified",
          await s.pending() == [] and (await s.cart()).is_empty)
    r = await s.say("add it")
    check("...and 'add it' over that list asks again instead of guessing",
          "Which one" in r.reply and await s.pending() == [] and (await s.cart()).is_empty)
    s = await Shopper(engine).start()
    await s.say("I need socks")
    r = await s.say("add that one")
    cart = await s.cart()
    check("ONE product on screen: 'add that one' resolves deterministically to that product and adds it (Step 25: an "
          "explicit command executes; nothing is left pending)",
          await s.pending() == [] and len(cart.lines) == 1 and "Socks" in cart.lines[0].title and cart.lines[0].quantity == 1)
    s = await Shopper(engine).start()
    await s.say("show me running shoes")
    r = await s.say("add something blue")
    check("with several shoes on screen, 'add something blue' still asks (does not pick one)",
          "not sure which product" in r.reply and await s.pending() == [])

    print("\n2. Stale proposal is not approved by typing")
    # Step 25: an explicit "add X" now executes, so a PENDING proposal (the fixture these approval behaviours need) comes from a non-explicit intent ("I'll take the X"), which is still only a proposal.
    from datetime import UTC, datetime, timedelta
    from sqlalchemy import update
    from engine.db.models import CartProposal
    from engine.db.session import session_scope

    s = await Shopper(engine).start()
    await s.say("I'll take the Performance Socks Three Pack")  # non-explicit: a proposal
    pend = await s.pending()
    check("setup: one pending proposal", len(pend) == 1)
    async with session_scope() as db:
        await db.execute(update(CartProposal).where(CartProposal.proposal_id == pend[0]["proposal_id"])
                         .values(created_at=datetime.now(UTC) - timedelta(days=3)))
    r = await s.say("yes")
    check("an aged proposal is refused at approval time with an honest reply", "can't do that one anymore" in r.reply)
    check("...the cart is unchanged and the proposal is retired", (await s.cart()).is_empty and await s.pending() == [])
    r = await s.say("yes")
    check("...and a further 'yes' has nothing to approve", "nothing waiting" in r.reply.lower() and (await s.cart()).is_empty)

    # a bare "yes" is not clearly about an old proposal while ANOTHER question is open
    from engine.db import shopping_state as shopping_store

    s = await Shopper(engine).start()
    await s.say("I'll take the Performance Socks Three Pack")  # non-explicit: a proposal
    check("setup: one pending proposal", len(await s.pending()) == 1)
    await shopping_store.merge(s.sid, s.cid, pending_question={"kind": "open_question", "text": "scripted"})
    await s.say("yes")
    check("with another question open, a bare 'yes' does NOT approve the old proposal",
          (await s.cart()).is_empty and len(await s.pending()) == 1)

    # generic add never offers a sold-out product as an option
    s = await Shopper(engine).start()
    r = await s.say("add tennis shoes to my cart")
    check("a sold-out-only match is not offered as an option to add",
          "Court Classic" not in r.reply and r.products == [] and await s.pending() == [] and (await s.cart()).is_empty)

    print("\n3. Kettle & Bloom coffee intent")
    core_departments = {"Single origin", "Blends"}

    async def kettle(msg):
        k = await Shopper(engine, "conn_kettle").start()
        r = await k.say(msg)
        return k, r

    async def all_in_stock_coffee(r, ad):
        ok = True
        for p in r.products:
            full = await ad.get_product(p.product_id)
            ok = ok and bool(set(full.categories) & core_departments)
            ok = ok and (any(str(v.availability) != "OUT_OF_STOCK" for v in full.variants) if full.variants else str(full.availability) != "OUT_OF_STOCK")
        return ok

    k, r = await kettle("I need a fruity coffee")
    ad = k.ad
    check("'fruity coffee': in-stock coffee options are returned", len(r.products) >= 3 and await all_in_stock_coffee(r, ad))
    check("...none of them equipment, gifts or subscriptions",
          not any(w in " ".join(titles(r)) for w in ("Dripper", "Grinder", "Scale", "Gift", "Subscription", "Kettle ")))
    check("...and the reply says honestly that nothing is DESCRIBED as fruity (a preference, not proof)",
          "described as" in r.reply and "fruity" in r.reply)
    check("...it does NOT claim the shop doesn't sell coffee, and is not a processing failure",
          "doesn't seem to carry" not in r.reply and not bad(r))
    check("...and nothing was added or proposed", await k.pending() == [] and (await k.cart()).is_empty)

    k, r = await kettle("I want a chocolatey coffee")
    check("'chocolatey coffee': exactly the products whose own data says chocolate",
          sorted(titles(r)) == ["Brazil Cerrado Pulped Natural", "Morning Bell Espresso Blend"])
    k, r = await kettle("do you have a decaf coffee")
    check("'decaf coffee': the decaf (and only it)", titles(r) == ["Swiss Water Decaf"])
    k, r = await kettle("I need a blackcurrant coffee")
    check("an attribute whose only match is SOLD OUT says so and shows what is in stock",
          "Kenya Nyeri AA" in r.reply and "sold out" in r.reply
          and "Kenya Nyeri AA" not in titles(r) and len(r.products) >= 3)
    k, r = await kettle("I want a purple coffee")
    check("an attribute nothing carries is disclosed, not treated as 'not sold here'",
          "described as" in r.reply and "doesn't seem to carry" not in r.reply and len(r.products) >= 3)
    k, r = await kettle("I want coffee")
    check("plain 'coffee' returns coffee only (no equipment/gifts/subscriptions)",
          len(r.products) >= 5 and await all_in_stock_coffee(r, k.ad)
          and not any(w in " ".join(titles(r)) for w in ("Dripper", "Grinder", "Scale", "Gift", "Subscription")))
    k, r = await kettle("show me coffee beans")
    check("'coffee beans' is the same range", len(r.products) >= 5 and await all_in_stock_coffee(r, k.ad))
    k, r = await kettle("I want running shoes")
    check("Kettle still honestly does not carry running shoes", r.products == [] and "doesn't seem to carry" in r.reply)

    print("\n4. Cross-merchant: Northfield does not start offering coffee")
    for msg in ("I need a fruity coffee", "I want coffee", "do you have a decaf coffee"):
        n = await Shopper(engine, "conn_demo").start()
        r = await n.say(msg)
        check(f"Northfield {msg!r}: no products and 'doesn't carry' (coffee is not its range)",
              r.products == [] and "doesn't seem to carry" in r.reply)
    n = await Shopper(engine, "conn_demo").start()
    r = await n.say("show me running shoes")
    check("Northfield's own range is untouched (running shoes still found)", len(r.products) >= 5)

    print()
    print(f"  {failures} FAILURE(S)" if failures else "  all Step 19 conversation-gap checks passed")


asyncio.run(main())
sys.exit(1 if failures else 0)
