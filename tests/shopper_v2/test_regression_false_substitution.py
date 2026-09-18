"""False-substitution regression cases, transcribed from PROGRESS.md's
16-round hardening saga, run against `engine.shopper_v2.evidence.build_evidence`
- the Stage 1 Evidence layer wrapping the same `engine.execution.relevance`
functions that saga hardened. These were previously only re-verified by
hand each round; this makes them permanent.

No network, no LLM, no live services required - pure deterministic function
calls against constructed Product objects.

    python tests/shopper_v2/test_regression_false_substitution.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.models import Availability, Money, Product  # noqa: E402

from engine.shopper_v2.evidence import build_evidence  # noqa: E402


def _p(product_id: str, title: str, description: str, categories: list[str], price: str) -> Product:
    amount, currency = price.split()
    return Product(
        product_id=product_id,
        title=title,
        description=description,
        categories=categories,
        price=Money(amount=amount, currency=currency),
        availability=Availability.IN_STOCK,
    )


NORTHFIELD_CATALOG = [
    _p("P1", "Trailblazer Running Shoe", "A lightweight daily trainer.", ["Shoes", "Running"], "3999 INR"),
    _p("P2", "Insulated Water Bottle 750ml", "Keeps drinks cold for hours.", ["Accessories"], "899 INR"),
    _p("P3", "Marathon Pro Racing Shoe", "Race-day carbon plate shoe.", ["Shoes", "Running"], "8999 INR"),
]


def main() -> int:
    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
        if not ok:
            failures += 1

    # Case 1: "do you sell kayaks?" - the raw sentence used as a rescue
    # search, must not surface an unrelated "Insulated Water Bottle" as a
    # confirmed match.
    ev = build_evidence(
        query="do you sell kayaks?",
        raw_said="do you sell kayaks?",
        products=NORTHFIELD_CATALOG,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=NORTHFIELD_CATALOG,
        platform_unavailable=False,
    )
    check(
        "kayak/water-bottle: no product is confidently verified relevant",
        not any(p.relevance_verified for p in ev.products),
    )

    # Case 2: model invents proxy query "water sports accessories" for a
    # kayak need - must not confirm the water bottle either.
    ev2 = build_evidence(
        query="water sports accessories",
        raw_said="I need a kayak",
        products=NORTHFIELD_CATALOG,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=NORTHFIELD_CATALOG,
        platform_unavailable=False,
    )
    check(
        "kayak/invented-proxy-query: no product is confidently verified relevant",
        not any(p.relevance_verified for p in ev2.products),
    )

    # Case 3: model invents proxy query "water bottle" (subset overlap with
    # what the shopper actually said, "water sports accessories") - must
    # still not confirm the wrong, merely-word-sharing product.
    ev3 = build_evidence(
        query="water bottle",
        raw_said="water sports accessories",
        products=NORTHFIELD_CATALOG,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=NORTHFIELD_CATALOG,
        platform_unavailable=False,
    )
    check(
        "kayak/subset-overlap-proxy: no product is confidently verified relevant",
        not any(p.relevance_verified for p in ev3.products),
    )

    # Case 4 (control - must NOT regress): a genuine, specific product
    # query DOES verify correctly.
    ev4 = build_evidence(
        query="running shoes",
        raw_said="I need running shoes",
        products=NORTHFIELD_CATALOG,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=NORTHFIELD_CATALOG,
        platform_unavailable=False,
    )
    check(
        "control: a genuine running-shoe query verifies at least one real match",
        any(p.relevance_verified for p in ev4.products if p.product_id in ("P1", "P3")),
    )

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all false-substitution regression checks passed")
    print()
    return 1 if failures else 0


sys.exit(main())
