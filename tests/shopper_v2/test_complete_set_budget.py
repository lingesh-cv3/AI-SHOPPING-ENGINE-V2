"""The shared total budget is preserved and every suggestion states its trade-off (AC-9).

An optional extra states its LIVE price and what it does to the remaining budget (computed from the REAL cart); it never changes the
fits/tight/over verdict about the stated list; when it would leave too little for the rest it says so; a cheaper alternative states a
saving only when it is a live-checked alternative and is honest about whether the merchant confirms it; with no budget there are no
remaining/saving figures at all.

    python tests/shopper_v2/test_complete_set_budget.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_budget.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_budget.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import standard_catalogue, start_mission  # noqa: E402
from _cs_fixtures import FakeShop, advance, cs_catalogue, ids, product  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine.shopper_v2 import guided_mission as g

    print("1. An extra states its live price and the remaining-budget effect, from the REAL cart")
    shop = FakeShop(cs_catalogue())
    sid, _s, _t = await start_mission(shop, "I want shoes, socks and something for energy, total budget 9000")
    shop.products["X-SLIDE"] = product("X-SLIDE", "Recovery Slide", 1599)  # the live price moved since the catalogue was written
    adv = await advance(shop, sid, "S-A")  # 1999 really in the cart -> 9000 - 1999 = 7001 left
    text = adv[0]
    check("the extra shows the LIVE price (1599, not the old 1499)", "Recovery Slide (1599 INR)" in text and "1499" not in text, text[-520:])
    check("remaining goes from 7001 to 5402 (= 7001 - 1599), computed from the real cart", "from 7001 INR to 5402 INR" in text, text[-420:])
    check("it names the trade-off as an addition", "It adds 1599 INR" in text, text[-420:])

    print("\n2. An extra never changes the verdict about the stated list")
    plain = FakeShop(standard_catalogue())
    psid, _p, _pt = await start_mission(plain, "I want shoes, socks and something for energy, total budget 9000")
    padv = await advance(plain, psid, "S-A")
    with_extra = (g.BUDGET_TRACE.get() or {}).get("verdict")
    shop_b = FakeShop(cs_catalogue())
    bsid, _b, _bt = await start_mission(shop_b, "I want shoes, socks and something for energy, total budget 9000")
    await advance(shop_b, bsid, "S-A")
    check("the budget verdict is identical with and without a suggested extra", with_extra is not None and with_extra == (g.BUDGET_TRACE.get() or {}).get("verdict"),
          (with_extra, g.BUDGET_TRACE.get()))
    check("the verdict sentence is the same (the extra is stated separately)", "Good so far" in text and "Good so far" in padv[0], (text[-300:], padv[0][-300:]))

    print("\n3. When an extra would leave too little for the rest, it says so")
    tight = FakeShop(cs_catalogue())
    tsid, _t, _tt = await start_mission(tight, "I want shoes, socks and something for energy, total budget 3500")
    tadv = await advance(tight, tsid, "S-A")  # 3500 - 1999 = 1501 left; cheapest rest 499 + 899 = 1398; the slide (1499) would leave 2
    check("the 'too little left' sentence appears, with the cheapest remaining total", "leave too little for the rest of your list" in tadv[0] and "about 1398 INR" in tadv[0], tadv[0][-520:])
    check("and it is still labelled optional, not on the list", "Optional extra, not on your list" in tadv[0])

    print("\n4. A cheaper alternative states a saving, and is honest about compatibility")
    alt_shop = FakeShop(cs_catalogue())
    asid, _a, _at = await start_mission(alt_shop, "I want shoes, socks and something for energy, total budget 4300")
    a1 = await advance(alt_shop, asid, "E-2")  # the energy chew bar (899) is in the cart; shoes is now the open stage
    check("an alternative that comes from the live-checked list states its saving versus the option it replaces",
          "Alpha Running Shoes (1999 INR) is 3000 INR less than Bravo Running Shoes" in a1[0], a1[0][-620:])
    check("an alternative the merchant has NOT declared with the cart item says so", "the merchant hasn't confirmed it goes with your Energy Chew Bar" in a1[0], a1[0][-420:])
    alt2 = FakeShop(cs_catalogue())
    bsid2, _b2, _bt2 = await start_mission(alt2, "I want shoes, socks and something for energy, total budget 4300")
    a2 = await advance(alt2, bsid2, "K-1")  # the crew socks are in the cart and the shoe declares them
    check("an alternative the merchant DID declare with the cart item says that instead", "the merchant lists it as going with your Crew Socks" in a2[0], a2[0][-620:])

    print("\n5. No budget: price only, no remaining or saving figures")
    nb = FakeShop(cs_catalogue())
    nsid, _n, _nt = await start_mission(nb, "I want shoes, socks and something for energy")
    nadv = await advance(nb, nsid, "S-A")
    check("the extra shows its price but no remaining-budget or saving claim", "Recovery Slide (1499 INR)" in nadv[0] and "remaining budget" not in nadv[0]
          and "It adds" not in nadv[0] and "less than" not in nadv[0], nadv[0][-420:])

    print("\n6. With a budget, an extra that cannot be priced in the budget's currency is not offered")
    cur = cs_catalogue()
    cur[[p.product_id for p in cur].index("X-SLIDE")] = product("X-SLIDE", "Recovery Slide", 20, currency="USD")
    cshop = FakeShop(cur)
    csid, _c, _ct = await start_mission(cshop, "I want shoes, socks and something for energy, total budget 9000")
    cadv = await advance(cshop, csid, "S-A")
    check("a USD extra is not offered against an INR budget (no mixed-currency arithmetic)", "X-SLIDE" not in ids(cadv[1]) and "Recovery Slide" not in cadv[0],
          ids(cadv[1]))
    unpriced = cs_catalogue()
    unpriced[[p.product_id for p in unpriced].index("X-SLIDE")] = product("X-SLIDE", "Recovery Slide", None)
    ushop = FakeShop(unpriced)
    usid, _u, _ut = await start_mission(ushop, "I want shoes, socks and something for energy, total budget 9000")
    uadv = await advance(ushop, usid, "S-A")
    check("an UNPRICED extra is not offered when a budget exists (no arithmetic on a missing price)", "X-SLIDE" not in ids(uadv[1]), ids(uadv[1]))
    nbu = FakeShop(unpriced)
    nsid2, _nu, _nut = await start_mission(nbu, "I want shoes, socks and something for energy")
    nuadv = await advance(nbu, nsid2, "S-A")
    check("with NO budget an unpriced extra is shown by name only, with no price or arithmetic", "X-SLIDE" in ids(nuadv[1]) and "no listed price" in nuadv[0] and "It adds" not in nuadv[0],
          nuadv[0][-300:])


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
