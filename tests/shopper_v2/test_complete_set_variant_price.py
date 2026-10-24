"""An extra whose variants are priced differently must not show one confident price (review of 7a56676).

The adapter contract allows per-variant prices, and the shopper picks the size/option AFTER the extra is announced. So the extra's price
and its budget effect must be stated as "from <lowest buyable option>" with an honest note, never as the product-level price alone; a
buyable option with no usable price makes the figure unknown (skipped under a budget); out-of-stock options never set the price.

    python tests/shopper_v2/test_complete_set_variant_price.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_variant_price.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_variant_price.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402
from decimal import Decimal  # noqa: E402

from _budget_fixtures import start_mission  # noqa: E402
from _cs_fixtures import MSG, FakeShop, advance, cs_catalogue, ids  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def with_variants(rows, currency="INR"):
    """rows: [(variant_id, price|None, in_stock)] attached to the declared extra X-SLIDE (product-level price stays 1499)."""
    from shared.models.commerce import Availability, Money, ProductVariant

    cat = cs_catalogue()
    slide = next(p for p in cat if p.product_id == "X-SLIDE")
    slide.variants = [ProductVariant(variant_id=v, title=v, availability=Availability.IN_STOCK if stock else Availability.OUT_OF_STOCK,
                                     price=None if pr is None else Money(amount=Decimal(str(pr)), currency=currency)) for v, pr, stock in rows]
    return cat


async def extra_reply(rows, **kw):
    shop = FakeShop(with_variants(rows, **kw))
    sid, _s, _t = await start_mission(shop, MSG)
    return await advance(shop, sid, "S-A")


async def checks():
    print("1. Variants priced differently: the headline price is the lowest buyable option, labelled 'from'")
    a = await extra_reply([("S", 1499, True), ("L", 2299, True)])
    check("the extra is still offered", "X-SLIDE" in ids(a[1]), ids(a[1]))
    check("no bare product-level price claim: it says 'from 1499 INR' and that the price depends on the option",
          "(from 1499 INR, price depends on the option)" in a[0], a[0][-420:])
    check("the budget effect is a bound, not a fixed figure",
          "It adds at least 1499 INR; your remaining budget would go from 7001 INR to at most 5502 INR." in a[0], a[0][-420:])

    print("\n2. Product price is lower than every buyable option: the product-level 1499 is NOT shown")
    b = await extra_reply([("S", 1899, True), ("L", 2299, True)])
    check("the figure is the real lowest option (1899), never the product-level 1499", "1899 INR" in b[0] and "1499" not in b[0], b[0][-420:])

    print("\n3. Out-of-stock options never set the price")
    c = await extra_reply([("S", 999, False), ("L", 1499, True), ("XL", 1499, True)])
    check("only buyable options count: uniform 1499 among buyable ones is a plain price", "Recovery Slide (1499 INR)" in c[0] and "from" not in c[0].split("Recovery Slide")[1][:20], c[0][-420:])

    print("\n4. A buyable option with no usable price makes the figure unknown")
    d = await extra_reply([("S", 1499, True), ("L", None, True)])
    check("an option with no price of its own inherits the product price (1499), so the quote stays plain", "Recovery Slide (1499 INR)" in d[0], d[0][-300:])
    nop = with_variants([("S", None, True)])
    next(p for p in nop if p.product_id == "X-SLIDE").price = None
    shop_n = FakeShop(nop)
    sid_n, _sn, _tn = await start_mission(shop_n, MSG)
    dn = await advance(shop_n, sid_n, "S-A")
    check("no price anywhere: under a budget the extra is not offered", "X-SLIDE" not in ids(dn[1]) and "Recovery Slide" not in dn[0], (ids(dn[1]), dn[0][-300:]))
    e = await extra_reply([("S", 1499, True), ("L", 1499, True)], currency="USD")
    check("an option priced in another currency is not offered under a budget either", "X-SLIDE" not in ids(e[1]), ids(e[1]))

    print("\n5. Uniform variants and no variants are unchanged")
    f = await extra_reply([("S", 1499, True), ("L", 1499, True)])
    check("uniform prices read exactly as before", "(1499 INR) as going with your Alpha Running Shoes" in f[0] and "It adds 1499 INR;" in f[0], f[0][-420:])
    shop = FakeShop(cs_catalogue())
    sid, _s, _t = await start_mission(shop, MSG)
    g_ = await advance(shop, sid, "S-A")
    check("a product with no variants is unchanged", "(1499 INR) as going with your Alpha Running Shoes" in g_[0], g_[0][-300:])


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
