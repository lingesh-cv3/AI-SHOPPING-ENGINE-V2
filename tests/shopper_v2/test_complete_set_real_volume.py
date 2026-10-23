"""Real volume: a 40,000-product catalogue and an anchor that declares 500 complements (real-client condition 2).

The complete-the-set code must stay inside bounded reads: no catalogue search of its own, a constant number of live product reads whatever
the catalogue size, and a declared list of 500 ids truncated to a handful of fetches with at most two extras shown. STRUCTURAL check
against a fake platform; NOT a claim that a real 40k-SKU platform has been validated (PROGRESS.md still lists that as open).

    python tests/shopper_v2/test_complete_set_real_volume.py
"""
from __future__ import annotations

import os
import sys
import time

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cs_volume.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cs_volume.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import start_mission  # noqa: E402
from _cs_fixtures import FakeShop, ids, product  # noqa: E402

failures = 0
MSG = "I want shoes, socks and something for energy, total budget 90000"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


class BigShop(FakeShop):
    MAX_PAGE = 100

    async def search_products(self, query, limit=20, **kw):
        if limit > self.MAX_PAGE:
            raise AssertionError(f"unbounded search: limit={limit}")
        return await super().search_products(query, limit=limit, **kw)


def build(n_each):
    accessories = [product(f"ACC{i}", f"Accessory {i}", 100 + i) for i in range(500)]
    products = [product("ANCH", "Anchor Running Shoes", 2000, compatible_with=[a.product_id for a in accessories])]
    products += [product(f"SH{i}", f"Model {i} Running Shoes", 1000 + (i % 9000)) for i in range(n_each)]
    products += [product(f"SK{i}", f"Model {i} Socks", 200 + (i % 800)) for i in range(n_each)]
    products += [product(f"EN{i}", f"Model {i} Energy Gel", 300 + (i % 1500)) for i in range(n_each)]
    return products + accessories


async def run(n_each):
    from engine.shopper_v2 import guided_mission as g

    shop = BigShop(build(n_each))
    sid, _spec, turn = await start_mission(shop, MSG)
    before = dict(shop.calls)
    t0 = time.time()
    shop.put_in_cart("ANCH")
    adv = await g.advance_from_cart(adapter=shop, session_id=sid, connection_id="conn_demo", cart_id="C1")
    took = time.time() - t0
    gets = shop.calls["get_product"] - before.get("get_product", 0)
    searches = shop.calls["search_products"] - before.get("search_products", 0)
    return shop, adv, gets, searches, took, len(shop.products)


async def checks():
    print("1. 40,000+ products, an anchor declaring 500 complements")
    shop, adv, gets, searches, took, total = await run(13400)
    check("the catalogue really is over 40,000 products", total > 40000, total)
    check("the add-on reply was produced", adv is not None and "Optional extra" in adv[0], adv and adv[0][-250:])
    extra_cards = [c for c in ids(adv[1]) if c.startswith("ACC")]
    check("at most TWO extras are shown, though 500 are declared", 1 <= len(extra_cards) <= 2, extra_cards)
    check("the new code made ZERO catalogue searches during the add-on reply", searches == 0, searches)
    check("live product reads are bounded (anchors <= 6, <= 12 extra candidates, <= 12 partners, shown/alternatives)", gets <= 60, gets)
    check("it finishes in bounded time (< 20 s)", took < 20, round(took, 2))
    check("the reply does not claim to list everything the merchant declares", "all the" not in adv[0].lower() and "every" not in adv[0].lower(), adv[0][-250:])
    check("nothing was written to the platform", shop.write_attempts == [], shop.write_attempts)

    print("\n2. The read count does not depend on catalogue size")
    _s2, adv2, gets_small, searches_small, _t, total_small = await run(20)
    check("the same journey on a ~600-product catalogue makes the same number of live product reads", gets_small == gets and searches_small == 0 and total_small < 1000,
          (gets_small, gets, total_small))


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
