"""Phase 3, first slice: Comparison (`engine/shopper_v2/compare.py`).

Model-free (constructs `ReasoningService(None)`, the same rules-fallback
pattern `test_zero_data_platform_down.py` already uses) and adapter-free
(a tiny local stub implementing only `get_product`) - no network, no LLM
key required.

    python tests/shopper_v2/test_phase3_compare.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.models import Availability, Money, Product  # noqa: E402

from engine.reasoning.service import ReasoningService  # noqa: E402
from engine.shopper_v2.compare import run_compare_turn  # noqa: E402
from engine.shopper_v2.state import ShoppingStateV2  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class _StubAdapter:
    def __init__(self, catalog: dict[str, Product], *, fail_ids: set[str] | None = None):
        self._catalog = catalog
        self._fail_ids = fail_ids or set()

    async def get_product(self, product_id: str) -> Product:
        if product_id in self._fail_ids:
            raise RuntimeError("simulated fetch failure")
        return self._catalog[product_id]


def _p(pid: str, title: str, price: str) -> Product:
    amount, currency = price.split()
    return Product(
        product_id=pid,
        title=title,
        description=f"{title} description",
        price=Money(amount=amount, currency=currency),
        availability=Availability.IN_STOCK,
    )


async def fewer_than_two_active_products_is_honest() -> None:
    state = ShoppingStateV2(session_id="s", connection_id="c", active_products=["P1"])
    reply, evidence, products, used_model = await run_compare_turn(
        reasoning=ReasoningService(None),
        adapter=_StubAdapter({}),
        message="compare the top two",
        state=state,
    )
    check("fewer than 2 active_products: honest reply, no fabricated comparison", "don't have two" in reply)
    check("fewer than 2 active_products: no products returned", products == [])
    check("fewer than 2 active_products: no evidence entries", evidence.products == [])


async def two_real_products_are_fetched_and_verified() -> None:
    catalog = {
        "P1001": _p("P1001", "Trailblazer Running Shoe", "4299.00 INR"),
        "P1005": _p("P1005", "Fell Runner Trail Shoe", "6799.00 INR"),
    }
    state = ShoppingStateV2(
        session_id="s", connection_id="c", active_products=["P1001", "P1005"]
    )
    reply, evidence, products, used_model = await run_compare_turn(
        reasoning=ReasoningService(None),  # no client configured -> deterministic fallback
        adapter=_StubAdapter(catalog),
        message="compare the top two",
        state=state,
    )
    check("both real products fetched", {p.product_id for p in products} == {"P1001", "P1005"})
    check(
        "both fetched products verified relevant",
        all(e.relevance_verified for e in evidence.products) and len(evidence.products) == 2,
    )
    check("deterministic fallback reply mentions both titles", "Trailblazer" in reply and "Fell Runner" in reply)
    check("no model configured -> used_model is False", used_model is False)


async def a_failed_fetch_degrades_honestly() -> None:
    catalog = {"P1001": _p("P1001", "Trailblazer Running Shoe", "4299.00 INR")}
    state = ShoppingStateV2(
        session_id="s", connection_id="c", active_products=["P1001", "P9999"]
    )
    reply, evidence, products, used_model = await run_compare_turn(
        reasoning=ReasoningService(None),
        adapter=_StubAdapter(catalog, fail_ids={"P9999"}),
        message="compare the top two",
        state=state,
    )
    check("a failed fetch does not raise or fabricate the missing product", len(products) == 1)
    check("no confident comparison when only one product could be fetched", evidence.products == [])
    check("honest reply on partial fetch failure", "couldn't fetch" in reply)


async def duplicate_ids_do_not_compare_a_product_against_itself() -> None:
    catalog = {"P1001": _p("P1001", "Trailblazer Running Shoe", "4299.00 INR")}
    state = ShoppingStateV2(
        session_id="s", connection_id="c", active_products=["P1001", "P1001"]
    )
    reply, evidence, products, used_model = await run_compare_turn(
        reasoning=ReasoningService(None),
        adapter=_StubAdapter(catalog),
        message="compare the top two",
        state=state,
    )
    check(
        "a duplicated single active product is not compared against itself",
        "don't have two" in reply and products == [],
    )


def main() -> int:
    asyncio.run(fewer_than_two_active_products_is_honest())
    asyncio.run(two_real_products_are_fetched_and_verified())
    asyncio.run(a_failed_fetch_degrades_honestly())
    asyncio.run(duplicate_ids_do_not_compare_a_product_against_itself())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 3 comparison checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
