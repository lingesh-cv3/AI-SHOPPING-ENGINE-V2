"""Phase 3 continuation: Compatibility + Complementary
(`engine/shopper_v2/compatibility.py`). Model-free (stub adapter/reasoning),
reuses `engine.execution.product_relations` unmodified.

    python tests/shopper_v2/test_phase3_compatibility.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.models import Availability, Money, Product  # noqa: E402

from engine.reasoning.service import ReasoningService  # noqa: E402
from engine.shopper_v2.compatibility import (  # noqa: E402
    run_compatibility_turn,
    run_complementary_turn,
)
from engine.shopper_v2.state import ShoppingStateV2  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


def _p(pid: str, title: str, compatible_with: list[str] | None = None) -> Product:
    return Product(
        product_id=pid, title=title,
        price=Money(amount="1000.00", currency="INR"),
        availability=Availability.IN_STOCK,
        compatible_with=compatible_with or [],
    )


class _StubAdapter:
    def __init__(self, catalog: dict[str, Product]):
        self._catalog = catalog

    async def get_product(self, product_id: str) -> Product:
        return self._catalog[product_id]


async def compatible_pair_confirmed() -> None:
    catalog = {
        "P1002": _p("P1002", "Marathon Pro Racing Shoe", compatible_with=["P3007"]),
        "P3007": _p("P3007", "Compression Calf Sleeves"),
    }
    state = ShoppingStateV2(session_id="s", connection_id="c", active_products=["P1002", "P3007"])
    reply, evidence, products, used_model = await run_compatibility_turn(
        reasoning=ReasoningService(None), adapter=_StubAdapter(catalog), message="do they work together?", state=state,
    )
    check("declared-compatible pair confirmed", "compatible" in reply.lower())
    check("both products in evidence, verified", all(e.relevance_verified for e in evidence.products))


async def undeclared_pair_reads_as_unknown_not_a_false_negative() -> None:
    catalog = {
        "P1001": _p("P1001", "Trailblazer Running Shoe"),
        "P3001": _p("P3001", "Insulated Water Bottle 750ml"),
    }
    state = ShoppingStateV2(session_id="s", connection_id="c", active_products=["P1001", "P3001"])
    reply, evidence, products, used_model = await run_compatibility_turn(
        reasoning=ReasoningService(None), adapter=_StubAdapter(catalog), message="compatible?", state=state,
    )
    check("undeclared relationship never asserts incompatibility", "not compatible" not in reply.lower())
    check("undeclared relationship is honestly unconfirmed", "hasn't been confirmed" in reply or "confirmed" in reply.lower())


async def fewer_than_two_products_is_honest() -> None:
    state = ShoppingStateV2(session_id="s", connection_id="c", active_products=["P1001"])
    reply, evidence, products, used_model = await run_compatibility_turn(
        reasoning=ReasoningService(None), adapter=_StubAdapter({}), message="compatible?", state=state,
    )
    check("fewer than two products -> honest decline, no fabricated verdict", "don't have two" in reply)


async def complementary_lists_real_declared_products() -> None:
    catalog = {
        "P1001": _p("P1001", "Trailblazer Running Shoe", compatible_with=["P3002", "P1006"]),
        "P3002": _p("P3002", "Performance Socks Three Pack"),
        "P1006": _p("P1006", "Recovery Slide"),
    }
    state = ShoppingStateV2(session_id="s", connection_id="c", active_products=["P1001"])
    reply, evidence, products, used_model = await run_complementary_turn(
        reasoning=ReasoningService(None), adapter=_StubAdapter(catalog), message="what else do I need?", state=state,
    )
    check("real declared complements named", "Performance Socks" in reply or "Recovery Slide" in reply)
    check("3 products total in evidence (anchor + 2 complements)", len(evidence.products) == 3)


async def no_declared_complements_is_honest_not_invented() -> None:
    catalog = {"P3001": _p("P3001", "Insulated Water Bottle 750ml")}
    state = ShoppingStateV2(session_id="s", connection_id="c", active_products=["P3001"])
    reply, evidence, products, used_model = await run_complementary_turn(
        reasoning=ReasoningService(None), adapter=_StubAdapter(catalog), message="what goes with this?", state=state,
    )
    check("no declared complements -> honest, no invented suggestion", "don't have any declared" in reply)


async def no_product_in_view_is_honest() -> None:
    state = ShoppingStateV2(session_id="s", connection_id="c")
    reply, evidence, products, used_model = await run_complementary_turn(
        reasoning=ReasoningService(None), adapter=_StubAdapter({}), message="complete the set", state=state,
    )
    check("no active product -> honest decline", "don't have a product" in reply)


def main() -> int:
    asyncio.run(compatible_pair_confirmed())
    asyncio.run(undeclared_pair_reads_as_unknown_not_a_false_negative())
    asyncio.run(fewer_than_two_products_is_honest())
    asyncio.run(complementary_lists_real_declared_products())
    asyncio.run(no_declared_complements_is_honest_not_invented())
    asyncio.run(no_product_in_view_is_honest())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all compatibility/complementary checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
