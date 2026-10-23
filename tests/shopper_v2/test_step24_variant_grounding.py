"""A word that only product VARIANTS can ground ("beans") is resolved from catalogue evidence, not guessed.

Step 24 design follow-up to Defect 2 (Step 22/23). On Kettle & Bloom the word "beans" is in no product title,
category or description, and the platform's keyword search does not index variant options - but every coffee
sells in a "250g whole bean" / "1kg whole bean" variant (`variant.attributes = {"format": "250g whole bean"}`).
That is real catalogue evidence, so the word is grounded deterministically: products having a variant whose
option text carries the word, from a bounded browse sample, through the SAME hard filters (stock, budget,
exclusions) as every other candidate. It is a last resort - after ordinary retrieval and any taxonomy-validated
categories, before the honest no-match. A word with no evidence anywhere is still an honest no-match and the
fallback judge is never asked to invent a pick.

The model is unavailable for every turn (deterministic path).

    python tests/shopper_v2/test_step24_variant_grounding.py   (needs Northfield on 8001, Kettle on 8002)
"""
from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_step24_variant_grounding.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_step24_variant_grounding.db"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool, got: object = None) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


# The seven coffees that are in stock (Kenya Nyeri AA is entirely out of stock).
BEANS = {"Ethiopia Guji Natural", "Colombia Huila Washed", "Brazil Cerrado Pulped Natural",
         "Morning Bell Espresso Blend", "Nightshift Dark Roast", "House Filter Blend", "Swiss Water Decaf"}


class Down:
    def __init__(self):
        self.judge_calls = 0

    async def complete(self, **kw):
        from engine.reasoning.llm import LLMUnavailable

        raise LLMUnavailable("no model in this test")


class Shopper:
    def __init__(self, eng, cid="conn_kettle", adapter=None):
        from engine.reasoning import ReasoningService

        self.eng, self.cid = eng, cid
        self.sid = f"s24v_{uuid.uuid4().hex[:8]}"
        self.ad = adapter or eng.registry.adapter_for(cid)
        self.rs = ReasoningService(Down())

    async def say(self, message):
        from engine.shopper_v2.graph import run_turn_via_graph

        return await run_turn_via_graph(
            reasoning=self.rs, adapter=self.ad, session_id=self.sid, connection_id=self.cid, message=message,
            cart_id=None, decision=self.eng.decision, gate=self.eng.gate, execution=self.eng.execution)


def titles(r):
    return [p.title for p in r.products]


async def scenarios(eng):
    print("1. 'beans' on Kettle is grounded in variant options")
    for q in ("beans", "bean", "show me beans", "whole beans"):
        r = await Shopper(eng).say(q)
        check(f"{q!r} -> exactly the in-stock coffees", set(titles(r)) == BEANS and len(titles(r)) == len(BEANS), titles(r))
    r = await Shopper(eng).say("beans")
    check("the trace says how it was grounded", (r.trace.get("query_normalization") or {}).get("outcome") == "variant_grounded",
          r.trace.get("query_normalization"))
    check("out-of-stock Kenya Nyeri AA is never offered", "Kenya Nyeri AA" not in titles(r))
    check("no equipment, gifts or subscriptions (they have no such variant)", all(t in BEANS for t in titles(r)))

    print("\n2. Other variant words ground the same way (general, not a 'beans' patch)")
    r = await Shopper(eng).say("ground")
    check("'ground' -> products with a ground variant in stock (never equipment)",
          len(titles(r)) >= 4 and set(titles(r)) <= BEANS, titles(r))

    print("\n3. Budget / hard filters still apply to the grounded candidates")
    r = await Shopper(eng).say("beans under 100")
    check("an impossible budget leaves nothing (no over-budget coffee is shown)", titles(r) == [], titles(r))

    print("\n4. No evidence anywhere: an honest no-match, no manufactured product, the judge never invents a pick")
    r = await Shopper(eng).say("flibbertigibbet")
    check("an unknown word yields nothing", titles(r) == [], titles(r))
    check("and says so honestly (no invented product)", "couldn't find anything" in r.reply and "Footwear" not in r.reply, r.reply[:160])
    r = await Shopper(eng, "conn_demo").say("beans")
    check("Northfield has no 'bean' variant: nothing is grounded there (per-merchant vocabulary)",
          titles(r) == [] and "couldn't find anything" in r.reply, (titles(r), r.reply[:120]))

    print("\n5. Ordinary retrieval is unaffected")
    for q, want in (("show me coffee", BEANS), ("coffee blends", {"Morning Bell Espresso Blend", "Nightshift Dark Roast",
                                                                "House Filter Blend", "Swiss Water Decaf"})):
        r = await Shopper(eng).say(q)
        check(f"{q!r} -> its real products", set(titles(r)) == want, titles(r))
    r = await Shopper(eng).say("Swiss Water Decaf")
    check("a named product still returns that one product", titles(r) == ["Swiss Water Decaf"], titles(r))
    r = await Shopper(eng, "conn_demo").say("show me running shoes")
    check("Northfield search unaffected (size variants are numbers, not words)", len(titles(r)) >= 2, titles(r))

    print("\n6. Empty catalogue / platform trouble: no crash, no product")

    class EmptyAdapter:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def search_products(self, *a, **kw):
            class R:
                products: list = []
                total_available = 0
            return R()

    r = await Shopper(eng, adapter=EmptyAdapter(eng.registry.adapter_for("conn_kettle"))).say("beans")
    check("empty catalogue: no products, a reply is still given", titles(r) == [] and bool(r.reply), (titles(r), r.reply[:100]))

    class Boom(EmptyAdapter):
        async def search_products(self, *a, **kw):
            raise RuntimeError("platform down")

    r = await Shopper(eng, adapter=Boom(eng.registry.adapter_for("conn_kettle"))).say("beans")
    check("platform down: the turn degrades (a reply, no product, no crash)", titles(r) == [] and bool(r.reply), r.reply[:100])

    print("\n6b. Review findings: variant-level stock, and a request that is NOT purely a variant word")
    from decimal import Decimal

    from engine.shopper_v2 import vocabulary as vocab_mod
    from shared.models import Availability, Money, Product, ProductVariant

    def prod(pid, title, *variants):
        return Product(
            product_id=pid, title=title, categories=["Bags"], availability=Availability.IN_STOCK,
            price=Money(amount=Decimal("500"), currency="INR"),
            variants=[ProductVariant(variant_id=f"{pid}-{i}", title=name, attributes={"colour": name},
                                     availability=avail) for i, (name, avail) in enumerate(variants)])

    catalogue = [
        prod("T1", "Trail Pack", ("red", Availability.IN_STOCK), ("blue", Availability.IN_STOCK)),
        prod("M1", "Camp Mug", ("red", Availability.OUT_OF_STOCK), ("blue", Availability.IN_STOCK)),
        prod("B1", "Day Tote", ("blue", Availability.IN_STOCK)),
    ]

    class Synthetic:
        """A store whose keyword search is title-only (like the demo platforms) and whose options carry colours."""

        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def search_products(self, query="", *a, **kw):
            class R:
                total_available = 0
            words = [w for w in (query or "").lower().split() if len(w) > 2]
            R.products = [p for p in catalogue if not words or any(w in p.title.lower() for w in words)]
            R.total_available = len(R.products)
            return R()

    vocab_mod._CACHE.pop("conn_demo", None)
    try:
        s = Shopper(eng, "conn_demo", adapter=Synthetic(eng.registry.adapter_for("conn_demo")))
        r = await s.say("red")
        check("'red' grounds in colour options and offers only the product whose RED variant is in stock",
              titles(r) == ["Trail Pack"], titles(r))
        vocab_mod._CACHE.pop("conn_demo", None)
        s = Shopper(eng, "conn_demo", adapter=Synthetic(eng.registry.adapter_for("conn_demo")))
        r = await s.say("do you have a red kayak")
        check("'a red kayak' is NOT answered with red things: the unknown word blocks the variant grounding",
              titles(r) == [], titles(r))
        check("...and the store does not appear to carry a kayak", "kayak" not in " ".join(titles(r)).lower())
        vocab_mod._CACHE.pop("conn_demo", None)
        s = Shopper(eng, "conn_demo", adapter=Synthetic(eng.registry.adapter_for("conn_demo")))
        r = await s.say("red tote")
        check("a request with a title word ('tote') is left to ordinary retrieval, not variant grounding",
              "Trail Pack" not in titles(r) and "Camp Mug" not in titles(r), titles(r))

        # Every known variant word must be carried by ONE variant. "ground" also appears in ANOTHER product's
        # description (so it is not variant-only), and no variant of House Roast is both "whole" and "ground".
        def plain(pid, title, desc, *variants):
            return Product(product_id=pid, title=title, description=desc, categories=["Coffee"],
                           availability=Availability.IN_STOCK, price=Money(amount=Decimal("500"), currency="INR"),
                           variants=[ProductVariant(variant_id=f"{pid}-{i}", title=n, attributes={"format": n},
                                                    availability=Availability.IN_STOCK) for i, n in enumerate(variants)])

        catalogue[:] = [plain("C1", "House Roast", "Roasted fresh", "whole bean", "ground"),
                        plain("G1", "Burr Mill", "Makes a consistent grind for ground coffee")]
        vocab_mod._CACHE.pop("conn_demo", None)
        s = Shopper(eng, "conn_demo", adapter=Synthetic(eng.registry.adapter_for("conn_demo")))
        r = await s.say("whole beans")
        check("'whole beans' grounds (one variant carries 'whole' and 'bean')", titles(r) == ["House Roast"], titles(r))
        vocab_mod._CACHE.pop("conn_demo", None)
        s = Shopper(eng, "conn_demo", adapter=Synthetic(eng.registry.adapter_for("conn_demo")))
        r = await s.say("ground beans")
        check("'ground beans' does NOT ground on 'beans' alone (no single variant is both ground and bean)",
              "House Roast" not in titles(r), titles(r))
    finally:
        vocab_mod._CACHE.pop("conn_demo", None)

    print("\n7. Vocabulary units")
    from engine.shopper_v2 import vocabulary
    ad = eng.registry.adapter_for("conn_kettle")
    res = await ad.search_products("", limit=100)
    vocab = vocabulary.build_vocabulary(list(getattr(res, "products", res)), None, "Kettle & Bloom Coffee")
    check("variant stems include 'bean' and 'ground'", {"bean", "ground"} <= vocab.variant_stems, sorted(vocab.variant_stems)[:12])
    check("variant words are not spelling targets", "beans" not in vocab.spell_words and "bean" not in vocab.all_stems)
    check("'beans' is a variant-only word; 'coffee blends' words are not",
          vocabulary.variant_only_words("beans", vocab) == ["beans"] and vocabulary.variant_only_words("blends", vocab) == [])
    check("numbers and units never become variant words", not any(any(ch.isdigit() for ch in s) for s in vocab.variant_stems))


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
