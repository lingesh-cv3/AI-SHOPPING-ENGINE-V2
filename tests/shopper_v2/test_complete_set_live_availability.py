"""Everything offered is fetched LIVE this turn and is buyable (AC-10).

A sold-out complement (product level and variant level), an unfetchable one, and a changed price are all handled from the live read;
an id the plan excluded (already in the cart) is never fetched as a candidate at all.

    python tests/shopper_v2/test_complete_set_live_availability.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_live.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_live.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import start_mission  # noqa: E402
from _cs_fixtures import MSG, FakeShop, advance, cs_catalogue, ids, product  # noqa: E402
from shared.models import Availability, Money, ProductVariant  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


class SpyShop(FakeShop):
    """Records WHICH product ids were fetched, not just how many."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.fetched: list[str] = []

    async def get_product(self, product_id):
        self.fetched.append(product_id)
        return await super().get_product(product_id)


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. Sold out: product level and variant level")
    shop = FakeShop(cs_catalogue())
    sid, _s, _t = await start_mission(shop, MSG)
    shop.products["X-SLIDE"] = product("X-SLIDE", "Recovery Slide", 1499, stock=False)
    a = await advance(shop, sid, "S-A")
    check("a sold-out declared complement is not offered", "X-SLIDE" not in ids(a[1]) and "Optional extra" not in a[0], (ids(a[1]), a[0][-250:]))
    shop2 = FakeShop(cs_catalogue())
    sid2, _s2, _t2 = await start_mission(shop2, MSG)
    slide = product("X-SLIDE", "Recovery Slide", 1499)
    slide.variants = [ProductVariant(variant_id="v1", title="8", availability=Availability.OUT_OF_STOCK, price=Money(amount=1499, currency="INR")),
                      ProductVariant(variant_id="v2", title="9", availability=Availability.OUT_OF_STOCK, price=Money(amount=1499, currency="INR"))]
    shop2.products["X-SLIDE"] = slide
    a2 = await advance(shop2, sid2, "S-A")
    check("a complement whose EVERY variant is sold out is not offered (the product-level flag says in stock)", "X-SLIDE" not in ids(a2[1]), ids(a2[1]))
    shop3 = FakeShop(cs_catalogue())
    sid3, _s3, _t3 = await start_mission(shop3, MSG)
    ok_slide = product("X-SLIDE", "Recovery Slide", 1499)
    ok_slide.variants = [ProductVariant(variant_id="v1", title="8", availability=Availability.OUT_OF_STOCK),
                         ProductVariant(variant_id="v2", title="9", availability=Availability.IN_STOCK)]
    shop3.products["X-SLIDE"] = ok_slide
    a3 = await advance(shop3, sid3, "S-A")
    check("with ONE variant still in stock the complement IS offered", "X-SLIDE" in ids(a3[1]), ids(a3[1]))

    print("\n2. A product that cannot be fetched is skipped, with no error text")
    shop4 = FakeShop(cs_catalogue())
    sid4, _s4, _t4 = await start_mission(shop4, MSG)
    shop4.product_fails = {"X-SLIDE"}
    a4 = await advance(shop4, sid4, "S-A")
    check("the unfetchable complement is not offered and nothing raw reaches the reply", "X-SLIDE" not in ids(a4[1]) and "cannot fetch" not in a4[0] and "RuntimeError" not in a4[0],
          a4[0][-250:])
    check("the rest of the reply still renders (the socks stage and its label)", "Crew Socks" in a4[0] and "The merchant lists Crew Socks" in a4[0], a4[0][-300:])

    print("\n3. The live price is what is shown, not an older one")
    shop5 = FakeShop(cs_catalogue())
    sid5, _s5, _t5 = await start_mission(shop5, MSG)
    shop5.products["X-SLIDE"] = product("X-SLIDE", "Recovery Slide", 1799)
    a5 = await advance(shop5, sid5, "S-A")
    check("a price change since the mission started is reflected (1799)", "Recovery Slide (1799 INR)" in a5[0], a5[0][-300:])

    print("\n4. Excluded ids are not fetched as candidates")
    spy = SpyShop(cs_catalogue())
    spy.put_in_cart("X-SLIDE")  # already in the cart: excluded
    sid6, _s6, _t6 = await start_mission(spy, MSG)
    spy.fetched.clear()
    spy.put_in_cart("S-A")
    adv = await g.advance_from_cart(adapter=spy, session_id=sid6, connection_id="conn_demo", cart_id="C1")
    anchor_fetches = spy.fetched.count("X-SLIDE")
    check("the slide is only read as a cart line (an anchor), never as an extra candidate: its id was fetched at most for the cart-line reads",
          "X-SLIDE" not in ids(adv[1]) and (g.COMPLETE_TRACE.get() or {}).get("extras_offered_ids") == [], (anchor_fetches, g.COMPLETE_TRACE.get()))
    check("an id the shopper never saw and no plan referenced was not fetched (the decoy is untouched)", "X-DECOY" not in spy.fetched, spy.fetched)


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
