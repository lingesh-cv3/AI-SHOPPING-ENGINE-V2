"""Real volume: a 40,000-product catalogue (AC real-client condition 2).

The budget code must stay inside bounded queries: it only uses what a stage's own search returned (<= 12 candidates per stage, <= 6
stages), at most 6 live product reads for alternatives, and the cart lines. A search wider than a page, or anything that reads "all"
of the catalogue, fails this test. This is a STRUCTURAL check against a fake platform; it is NOT a claim that a real 40k-SKU platform
has been validated (PROGRESS.md still lists that as open).

    python tests/shopper_v2/test_budget_real_volume.py
"""
from __future__ import annotations

import os
import sys
import time

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_budget_volume.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_budget_volume.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _budget_fixtures import FakeShop, product, start_mission  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


class BigShop(FakeShop):
    """A 40,000-product platform that refuses anything wider than one page of search results."""

    MAX_PAGE = 100

    async def search_products(self, query, limit=20, **kw):
        if limit > self.MAX_PAGE:
            raise AssertionError(f"unbounded search: limit={limit}")
        return await super().search_products(query, limit=limit, **kw)


async def checks():
    print("1. 40,000 products, a 3-stage budgeted mission")
    products = []
    for i in range(14000):
        products.append(product(f"SH{i}", f"Model {i} Running Shoes", 1000 + (i % 9000)))
    for i in range(13000):
        products.append(product(f"SK{i}", f"Model {i} Socks", 200 + (i % 800)))
    for i in range(13000):
        products.append(product(f"EN{i}", f"Model {i} Energy Gel", 300 + (i % 1500)))
    shop = BigShop(products)
    check("the catalogue really has 40,000 products", len(shop.products) == 40000, len(shop.products))
    t0 = time.time()
    sid, spec, turn = await start_mission(shop, "I want shoes, socks and something for energy, total budget 6000")
    took = time.time() - t0
    check("the mission starts and the verdict is computed", turn is not None and (turn.budget_trace or {}).get("verdict") in ("fits", "tight", "over"),
          turn and turn.budget_trace)
    check("at most one search per stage (3), none wider than a page", shop.calls["search_products"] <= 3, dict(shop.calls))
    check("live product reads are bounded (shown options + <= 6 alternatives + stage cards), never a scan",
          shop.calls["get_product"] <= 6 * (4 + 6) + 6, dict(shop.calls))
    check("the estimate used only the stored prices of <= 12 candidates per stage",
          all(len(i.candidate_prices) <= 12 for i in turn.state.mission_items), [len(i.candidate_prices) for i in turn.state.mission_items])
    check("it finishes in bounded time (< 20 s on this machine)", took < 20, round(took, 2))

    print("\n2. The same mission turn after turn stays bounded")
    from engine.shopper_v2 import guided_mission as g

    before = dict(shop.calls)
    t = await g.handle_command(adapter=shop, command="next", message="next", state=turn.state, cart_id="C1", memory_facts=None)
    added_gets = shop.calls["get_product"] - before.get("get_product", 0)
    check("a follow-up turn makes no new catalogue search at all", shop.calls["search_products"] == before.get("search_products", 0), dict(shop.calls))
    check("and only a bounded number of product reads", added_gets <= 6 * 4 + 6 + 6, added_gets)
    check("the follow-up carries the same honest verdict", t is not None and (t.budget_trace or {}).get("verdict") in ("fits", "tight", "over"), t and t.budget_trace)


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
