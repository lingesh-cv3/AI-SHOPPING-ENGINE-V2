"""Store-level discovery: "what do you have?" answers with the shop's real categories as choices.

User-reported (2026-10-21 manual test): "what do you have?" was run as a literal catalogue search for those
words and answered "I couldn't find anything matching that search right now" with only a footwear list.

Asserted:
  detector       broad questions about the offering match; any named product/category/attribute, a greeting,
                 or "what ELSE do you have" (alternatives) does not
  grounded       Northfield -> its six declared departments (Accessories, Apparel, Footwear, Nutrition,
                 Recovery, Tech); Kettle -> its five; never a list from anywhere else (merchant isolation)
  some / none    a merchant that carries only some departments shows only those; a department with nothing
                 buyable is not offered; an empty or failing catalogue gets an honest sentence and no choices
  selection      tapping a category ("Show me <category>") returns that category's real, in-stock products
  not a browse   a specific request still searches; the model calling the question "conversational" cannot
                 turn it into a no-match

The model is unavailable (deterministic path) except where a scripted claim is injected on purpose.

    python tests/shopper_v2/test_store_discovery.py   (needs Northfield on 8001, Kettle on 8002)
"""
from __future__ import annotations

import os
import sys
import uuid

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_store_discovery.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_store_discovery.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


NORTHFIELD = ["Accessories", "Apparel", "Footwear", "Nutrition", "Recovery", "Tech"]
KETTLE = ["Blends", "Equipment", "Gifts", "Single origin", "Subscriptions"]


def claim(**kw):
    base = {"capability": "discover", "context_action": "continue", "question_type": "retrieve", "reference_scope": "none",
            "reference_kind": "none", "conversational_repair": "none", "cart_operation": "none", "rationale": "scripted"}
    base.update(kw)
    return base


class Client:
    """Interpretation: the queued scripted claim (None = unavailable). Every other model call: unavailable."""

    def __init__(self):
        self.queue = []

    async def complete(self, *, system, messages, tools=None, force_tool=False):
        from engine.reasoning.llm import LLMResult, LLMUnavailable

        name = tools[0]["function"]["name"] if tools else None
        if name == "interpret_conversation" and self.queue:
            item = self.queue.pop(0)
            if item is not None:
                return LLMResult(text=None, model="scripted", tool_calls=[{"name": name, "arguments": item}])
        raise LLMUnavailable("scripted: no model")


class Shopper:
    def __init__(self, eng, cid="conn_demo", adapter=None):
        from engine.reasoning import ReasoningService

        self.eng, self.cid = eng, cid
        self.sid = f"sd_{uuid.uuid4().hex[:8]}"
        self.ad = adapter or eng.registry.adapter_for(cid)
        self.client = Client()
        self.rs = ReasoningService(self.client)

    async def say(self, message, model_claim=None):
        from engine.shopper_v2.graph import run_turn_via_graph

        self.client.queue.append(model_claim)
        return await run_turn_via_graph(
            reasoning=self.rs, adapter=self.ad, session_id=self.sid, connection_id=self.cid, message=message,
            cart_id=None, decision=self.eng.decision, gate=self.eng.gate, execution=self.eng.execution)


def choices(r):
    return list(r.trace.get("category_choices") or [])


def titles(r):
    return [p.title for p in r.products]


async def main_checks(eng):
    from engine.shopper_v2.store_discovery import looks_like_store_discovery as f

    print("1. Detector: broad questions about the offering")
    for m in ("what do you have?", "What do you have", "what do you sell", "what do you sell here?", "what's available",
              "what is available", "what can I buy here", "show me everything", "what products do you have",
              "do you have anything", "what kind of stuff do you carry?", "browse the store", "list your categories",
              "what are your categories", "what do you offer", "what's in the store", "what all do you have",
              "tell me what you have", "what items do you stock"):
        check(f"store discovery: {m!r}", f(m) is True)
    print("   ... and the messages that must stay searches / conversation")
    for m in ("hi", "hello", "do you have tea?", "show me running shoes", "what do you have for running",
              "what do you have under 5000", "do you have coffee", "show me something", "I want to buy something",
              "what else do you have", "what other products do you have", "do you sell socks", "trailblazer",
              "what is the cheapest shoe", "add it to the cart", "yes", "", "what do you recommend"):
        check(f"NOT store discovery: {m!r}", f(m) is False)

    print("\n2. Grounded in the merchant's own departments, with a buyable product behind each")
    for cid, want, foreign in (("conn_demo", NORTHFIELD, KETTLE), ("conn_kettle", KETTLE, NORTHFIELD)):
        r = await Shopper(eng, cid).say("what do you have?")
        check(f"{cid}: exactly its declared departments", sorted(choices(r)) == sorted(want), choices(r))
        check(f"{cid}: no other merchant's category leaks in", not (set(choices(r)) & set(foreign)))
        check(f"{cid}: not the no-match reply", "couldn't find anything" not in r.reply, r.reply[:120])
        check(f"{cid}: no products invented, no state side effects on the cart", titles(r) == [])
        check(f"{cid}: the reply names the categories it offers", all(c in r.reply for c in want), r.reply[:200])
    r = await Shopper(eng).say("what do you have?", claim(capability="conversational"))
    check("a model that calls it 'conversational' cannot turn it into a no-match", sorted(choices(r)) == sorted(NORTHFIELD), (choices(r), r.reply[:100]))
    r = await Shopper(eng).say("what do you have?", claim(capability="discover", question_type="retrieve"))
    check("a model that calls it a plain discovery search cannot either", sorted(choices(r)) == sorted(NORTHFIELD), choices(r))

    print("\n3. Selecting a category leads to that category's real, in-stock products")
    ad = eng.registry.adapter_for("conn_demo")
    for cat in NORTHFIELD:
        r = await Shopper(eng).say(f"Show me {cat}")
        prods = titles(r)
        inside = {p.product_id for p in list((await ad.search_products("", limit=60, dept=cat)).products)
                  if str(p.availability) != "OUT_OF_STOCK"}
        check(f"Northfield 'Show me {cat}' -> real in-stock products of that department",
              bool(prods) and {p.product_id for p in r.products} <= inside, (cat, prods[:4]))
    for cat in KETTLE:
        r = await Shopper(eng, "conn_kettle").say(f"Show me {cat}")
        check(f"Kettle 'Show me {cat}' -> products", bool(titles(r)), (cat, r.reply[:100]))

    print("\n4. Some / none / failing catalogues (a controlled platform)")
    from shared.models import Availability, Money, Product
    from decimal import Decimal

    def prod(pid, title, cat, avail=Availability.IN_STOCK):
        return Product(product_id=pid, title=title, categories=[cat], availability=avail,
                       price=Money(amount=Decimal("100"), currency="INR"))

    class Platform:
        def __init__(self, inner, departments, products, fail=False):
            self._inner, self.departments, self.products, self.fail = inner, departments, products, fail

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def list_departments(self):
            if self.fail:
                raise RuntimeError("platform down")
            return list(self.departments)

        async def search_products(self, query="", limit=20, dept=None, **kw):
            if self.fail:
                raise RuntimeError("platform down")

            class R:
                total_available = 0
            pool = [p for p in self.products if dept is None or dept in p.categories]
            words = [w for w in (query or "").lower().split() if len(w) > 2]
            R.products = [p for p in pool if not words or any(w in p.title.lower() for w in words)][:limit]
            R.total_available = len(R.products)
            return R()

    from engine.shopper_v2 import vocabulary

    inner = eng.registry.adapter_for("conn_demo")
    prods = [prod("A1", "Belt", "Accessories"), prod("F1", "Shoe", "Footwear"), prod("F2", "Boot", "Footwear"),
             prod("T1", "Watch", "Tech", Availability.OUT_OF_STOCK)]
    vocabulary._CACHE.pop("conn_demo", None)
    r = await Shopper(eng, adapter=Platform(inner, ["Accessories", "Apparel", "Footwear", "Tech"], prods)).say("what do you have?")
    check("only the categories with something buyable are offered (Apparel is empty, Tech is sold out)",
          choices(r) == ["Accessories", "Footwear"], choices(r))
    vocabulary._CACHE.pop("conn_demo", None)
    r = await Shopper(eng, adapter=Platform(inner, ["Footwear"], prods)).say("what do you have?")
    check("a single-department merchant shows that one", choices(r) == ["Footwear"], choices(r))
    vocabulary._CACHE.pop("conn_demo", None)
    r = await Shopper(eng, adapter=Platform(inner, ["Apparel", "Recovery"], prods)).say("what do you have?")
    check("none of the declared departments has stock: honest sentence, no choices, no products",
          choices(r) == [] and titles(r) == [] and bool(r.reply) and "couldn't find anything" not in r.reply, (choices(r), r.reply[:100]))
    check("...and the trace says why (empty)", r.trace.get("store_discovery") == "empty", r.trace.get("store_discovery"))
    vocabulary._CACHE.pop("conn_demo", None)
    r = await Shopper(eng, adapter=Platform(inner, [], [])).say("what do you have?")
    check("an empty catalogue: honest, no choices, no crash", choices(r) == [] and bool(r.reply), (choices(r), r.reply[:100]))
    vocabulary._CACHE.pop("conn_demo", None)
    r = await Shopper(eng, adapter=Platform(inner, ["Footwear"], prods, fail=True)).say("what do you have?")
    check("platform down: the turn degrades with an honest reply, no raw error, no choices",
          choices(r) == [] and bool(r.reply) and "platform down" not in r.reply and "Traceback" not in r.reply, r.reply[:140])
    check("...and the trace says why (unavailable)", r.trace.get("store_discovery") == "unavailable", r.trace.get("store_discovery"))
    vocabulary._CACHE.pop("conn_demo", None)

    print("\n5. A specific request is still a search, never a generic category browse")
    r = await Shopper(eng).say("show me running shoes")
    check("'show me running shoes' returns products and no category overview", len(titles(r)) >= 2 and choices(r) == [], (titles(r)[:3], choices(r)))
    r = await Shopper(eng).say("what do you have for running")
    check("'what do you have for running' is a search (running shoes), not the overview", choices(r) == [] and len(titles(r)) >= 1, (choices(r), titles(r)[:3]))
    r = await Shopper(eng, "conn_kettle").say("do you have tea?")
    check("'do you have tea?' keeps the honest not-carried answer", choices(r) == [] and titles(r) == [], (choices(r), titles(r)))

    print("\n6. The overview leaves no stray conversation state")
    s = Shopper(eng)
    await s.say("what do you have?")
    from engine.shopper_v2.state import load
    st = await load(s.sid, "conn_demo")
    labels = [f.label for f in (st.focus_stack or [])]
    check("no bogus topic was pushed for the question itself", not any("have" in (l or "").lower() for l in labels), labels)
    check("no product ids were recorded as 'shown'", not (st.last_rendered and st.last_rendered.product_ids), st.last_rendered)


async def main():
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401 - registers the V2 tables before create_schema

    await create_schema()
    eng = build_engine()
    try:
        await main_checks(eng)
    finally:
        await eng.close()
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
