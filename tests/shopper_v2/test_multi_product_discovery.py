"""Several products named in one request: every one found is shown, in the order named; absent ones are reported.

Known limitation closed (Step 25): "Show me the Trailblazer and the Starter Road Shoe" returned only the top-scoring
product, silently dropping the other. Each named part is now resolved on its own by COVERAGE (every distinctive word
of the part is a word of exactly one product's title), against the shop's own products only.

Asserted:
  two / three names     all shown, in the order the shopper named them
  partial               a named part the catalogue does not have is reported, never silently dropped, never invented
  spelling variants     a misspelt name still resolves when the shop's own vocabulary corrects it unambiguously
  duplicates            naming a product twice shows it once
  mixed availability    a sold-out requested product is reported as unavailable, not shown as buyable
  ambiguity             "running shoes" names no single product: never forced into one; a category request such as
                        "trail shoes and socks" is NOT turned into "just the socks"
  isolation             one merchant's request never returns another merchant's products
  unchanged             one named product and ordinary searches behave exactly as before

Model unavailable for every turn. Needs Northfield (8001) and Kettle (8002).

    python tests/shopper_v2/test_multi_product_discovery.py
"""
from __future__ import annotations

import os
import sys
import uuid

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_multi_product.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_multi_product.db"
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


class Down:
    async def complete(self, **kw):
        from engine.reasoning.llm import LLMUnavailable

        raise LLMUnavailable("no model in this test")


class Shopper:
    def __init__(self, eng, cid="conn_demo"):
        from engine.reasoning import ReasoningService

        self.eng, self.cid = eng, cid
        self.sid = f"mp_{uuid.uuid4().hex[:8]}"
        self.ad = eng.registry.adapter_for(cid)
        self.rs = ReasoningService(Down())

    async def say(self, message):
        from engine.shopper_v2.graph import run_turn_via_graph

        return await run_turn_via_graph(
            reasoning=self.rs, adapter=self.ad, session_id=self.sid, connection_id=self.cid, message=message,
            cart_id=None, decision=self.eng.decision, gate=self.eng.gate, execution=self.eng.execution)


def titles(r):
    return [p.title for p in r.products]


TB, SR = "Trailblazer Running Shoe", "Starter Road Running Shoe"
SOCKS, GEL = "Performance Socks Three Pack", "Energy Gel Box of 12"


async def checks(eng):
    print("1. Two named products: both, in the order named")
    r = await Shopper(eng).say("Show me the Trailblazer and the Starter Road Shoe")
    check("both products are shown", set(titles(r)) == {TB, SR}, titles(r))
    check("in the order the shopper named them (Trailblazer first)", titles(r) == [TB, SR], titles(r))
    r = await Shopper(eng).say("show me the starter road shoe and the trailblazer")
    check("the reverse order is preserved", titles(r) == [SR, TB], titles(r))
    for phrase in ("the Trailblazer & the Starter Road Shoe", "Trailblazer, Starter Road Shoe", "do you have the trailblazer plus the starter road shoe"):
        r = await Shopper(eng).say(phrase)
        check(f"{phrase!r}", titles(r) == [TB, SR], titles(r))

    print("\n2. Three named products")
    r = await Shopper(eng).say("Show me the Trailblazer, the Starter Road Shoe and the Performance Socks")
    check("all three, in order", titles(r) == [TB, SR, SOCKS], titles(r))

    print("\n3. Partial: a named part the catalogue does not have is reported")
    r = await Shopper(eng).say("Show me the Trailblazer and the Zorblax Hoverboard")
    check("the found one is shown", titles(r) == [TB], titles(r))
    check("the missing one is said plainly, not invented or dropped", "couldn't find" in r.reply.lower() and "zorblax" in r.reply.lower(), r.reply[:200])
    r = await Shopper(eng).say("Show me the Trailblazer, the Starter Road Shoe and the Zorblax Hoverboard")
    check("two found + one missing: both shown and the gap reported", titles(r) == [TB, SR] and "zorblax" in r.reply.lower(), (titles(r), r.reply[:160]))

    print("\n4. Spelling variants")
    r = await Shopper(eng).say("Show me the trailblazr and the starter road shoe")
    check("a misspelt name resolves through the shop's own vocabulary", titles(r) == [TB, SR], titles(r))

    print("\n5. Duplicates")
    r = await Shopper(eng).say("Show me the Trailblazer and the Trailblazer and the Starter Road Shoe")
    check("a product named twice is shown once", titles(r) == [TB, SR], titles(r))

    print("\n6. Mixed availability (a controlled catalogue)")
    from decimal import Decimal

    from engine.shopper_v2 import vocabulary
    from shared.models import Availability, Money, Product

    def prod(pid, title, avail=Availability.IN_STOCK):
        return Product(product_id=pid, title=title, categories=["Gear"], availability=avail,
                       price=Money(amount=Decimal("100"), currency="INR"))

    catalogue = [prod("A", "Alpine Pack"), prod("B", "Birch Mug", Availability.OUT_OF_STOCK), prod("C", "Cedar Lamp")]

    class Platform:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def search_products(self, query="", limit=20, dept=None, **kw):
            class R:
                total_available = 0
            words = [w for w in (query or "").lower().split() if len(w) > 2]
            R.products = [p for p in catalogue if not words or any(w in p.title.lower() for w in words)][:limit]
            R.total_available = len(R.products)
            return R()

    vocabulary._CACHE.pop("conn_demo", None)
    s = Shopper(eng)
    s.ad = Platform(eng.registry.adapter_for("conn_demo"))
    r = await s.say("Show me the Alpine Pack and the Birch Mug and the Cedar Lamp")
    check("the buyable ones are shown", titles(r) == ["Alpine Pack", "Cedar Lamp"], titles(r))
    check("the sold-out one is reported as unavailable, not shown as buyable", "birch mug" in r.reply.lower() and "not available" in r.reply.lower(), r.reply[:200])
    vocabulary._CACHE.pop("conn_demo", None)

    print("\n7. Ambiguous names are never forced into a product")
    r = await Shopper(eng).say("Show me running shoes and trail shoes")
    check("two category requests are not collapsed into specific products", len(titles(r)) != 2 or set(titles(r)) != {TB, SR}, titles(r))
    r = await Shopper(eng).say("show me trail shoes and socks")
    check("'trail shoes and socks' is NOT turned into 'just the socks'", titles(r) != [SOCKS], titles(r))

    print("\n8. Merchant isolation")
    r = await Shopper(eng, "conn_kettle").say("Show me the Trailblazer and the Starter Road Shoe")
    check("Northfield's products never appear at Kettle", not (set(titles(r)) & {TB, SR}), titles(r))
    r = await Shopper(eng, "conn_kettle").say("Show me Swiss Water Decaf and the Morning Bell Espresso Blend")
    check("Kettle resolves its own two products", titles(r) == ["Swiss Water Decaf", "Morning Bell Espresso Blend"], titles(r))

    print("\n9. Unchanged behaviour")
    r = await Shopper(eng).say("show me the trailblazer")
    check("one named product still returns it", TB in titles(r), titles(r))
    r = await Shopper(eng).say("show me running shoes under 5000")
    check("a budget category search is untouched", set(titles(r)) == {TB, SR}, titles(r))
    r = await Shopper(eng).say("show me socks and gel")
    check("two single-product category words still resolve to exactly those products",
          set(titles(r)) == {SOCKS, GEL}, titles(r))
    r = await Shopper(eng).say("I need shoes, socks and something for energy")
    check("a multi-category mission is still a mission (not turned into a product list)",
          r.trace.get("capability") == "mission", r.trace.get("capability"))


async def overlap_checks(eng):
    print("\n11. A missing name that shares words with the catalogue (Step 25, Priority 2)")
    r = await Shopper(eng).say("Show me the Trailblazer Running Shoe and the Zephyr Cloud Boot")
    check("the real product is shown, nothing substituted", titles(r) == [TB], titles(r))
    check("the unknown name is reported plainly", "couldn't find" in r.reply.lower() and "zephyr" in r.reply.lower(), r.reply[:200])
    r = await Shopper(eng).say("Show me the Zephyr Cloud Boot and the Trailblazer Running Shoe")
    check("same when the missing name comes first", titles(r) == [TB] and "zephyr" in r.reply.lower(), (titles(r), r.reply[:160]))
    r = await Shopper(eng).say("Show me the Trailblazer Running Shoe, the Starter Road Running Shoe and the Zephyr Cloud Boot")
    check("two found + one overlapping-name missing: both shown, gap reported",
          titles(r) == [TB, SR] and "zephyr" in r.reply.lower(), (titles(r), r.reply[:160]))

    print("\n12. Nothing legitimate regresses")
    r = await Shopper(eng).say("Show me the Trailblazr Running Shoe and the Starter Road Running Shoe")
    check("a spelling variant still resolves (the corrected word is not 'unknown')", titles(r) == [TB, SR], titles(r))
    r = await Shopper(eng).say("Show me the Trailblazer Running Shoe and the Starter Road Running Shoe")
    check("exact names unchanged", titles(r) == [TB, SR], titles(r))
    r = await Shopper(eng).say("Show me the Trailblazer Running Shoe and cheap socks")
    check("a lowercase modifier ('cheap') is not treated as a missing product name",
          "couldn't find" not in r.reply.lower(), r.reply[:200])
    r = await Shopper(eng).say("Cheap socks and the Trailblazer Running Shoe")
    check("a sentence-initial capital proves nothing", "couldn't find" not in r.reply.lower(), r.reply[:200])
    r = await Shopper(eng).say("Show me trail shoes and socks")
    check("a category request is still a category request", len(titles(r)) > 1 and "couldn't find" not in r.reply.lower(), titles(r))

    print("\n13. Known limitation (documented, not hidden): a name made ONLY of words the shop uses")
    r = await Shopper(eng).say("Show me the Trailblazer Running Shoe and the Cloud Boot")
    check("it degrades to the ordinary search without error or invented claims", "Traceback" not in r.reply and titles(r) != [],
          (titles(r), r.reply[:160]))


class SampledCatalogue:
    """Stands in for a catalogue far bigger than the vocabulary sample: the empty-query browse the vocabulary is built
    from returns only the first few products; any real query still reaches the whole catalogue."""

    def __init__(self, real, sample_size=5):
        self._real, self._n = real, sample_size

    def __getattr__(self, name):
        return getattr(self._real, name)

    async def search_products(self, query, *a, **kw):
        result = await self._real.search_products(query, *a, **kw)
        if not (query or "").strip() and not kw.get("dept"):
            products = list(getattr(result, "products", result) or [])[: self._n]
            try:
                return result.model_copy(update={"products": products})
            except AttributeError:
                return products
        return result


async def volume_checks(eng):
    from engine.shopper_v2 import vocabulary

    print("\n14. Large catalogue: a product outside the vocabulary sample is never reported 'couldn't find'")
    vocabulary._CACHE.pop("conn_demo", None)
    s = Shopper(eng)
    s.ad = SampledCatalogue(s.ad)
    try:
        r = await s.say("Show me the Trailblazer Running Shoe and the Performance Socks Three Pack")
        check("both real products found although 'socks' is not in the sampled vocabulary", titles(r) == [TB, SOCKS], titles(r))
        check("no false 'couldn't find'", "couldn't find" not in r.reply.lower(), r.reply[:200])
        r = await s.say("Show me the Trailblazer Running Shoe and sleeves")
        check("an unsampled word with SEVERAL real products behind it is never reported as missing",
              "couldn't find" not in r.reply.lower(), r.reply[:200])
        r = await s.say("Show me the Trailblazer Running Shoe and the Zorblax Hoverboard")
        check("a genuinely absent name is still reported after the catalogue search", titles(r) == [TB] and "zorblax" in r.reply.lower(),
              (titles(r), r.reply[:160]))
    finally:
        vocabulary._CACHE.pop("conn_demo", None)


async def main():
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401 - registers the V2 tables before create_schema

    await create_schema()
    eng = build_engine()
    try:
        await checks(eng)
        await overlap_checks(eng)
        await volume_checks(eng)
    finally:
        await eng.close()
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
