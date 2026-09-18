"""False-decline regression cases, transcribed from PROGRESS.md/Completed.md's
hardening-pass fixes to `engine.execution.mission_ranking` (hyphen/intensifier
matching, "-es"/"-ies" plural stemming) - a genuinely matching product must
not be wrongly excluded from the hard-filter/must-have shortlist just
because the shopper's phrasing and the merchant's prose differ
morphologically.

Run against `engine.shopper_v2.evidence.build_evidence`, the Stage 1 layer
wrapping `mission_ranking.rank_and_filter` unmodified. No network, no LLM,
no live services required.

    python tests/shopper_v2/test_regression_false_decline.py
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


CATALOG = [
    _p("P1", "Trailblazer Running Shoe", "A lightweight daily trainer.", ["Shoes", "Running"], "3999 INR"),
    _p("P2", "Cloudform Runner", "Featuring a cushioned midsole for daily miles.", ["Shoes", "Running"], "4599 INR"),
]


def main() -> int:
    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
        if not ok:
            failures += 1

    # Case 1: "ultra-lightweight" must-have must match a product described
    # only as "lightweight" - the intensifier prefix must not be demanded
    # literally.
    ev = build_evidence(
        query="lightweight running shoes",
        raw_said="I need ultra-lightweight running shoes",
        products=CATALOG,
        shopping_state={"must_have": ["ultra-lightweight"]},
        memory_facts=None,
        catalog_sample=CATALOG,
        platform_unavailable=False,
    )
    p1 = next(p for p in ev.products if p.product_id == "P1")
    check(
        "ultra-lightweight must-have matches a 'lightweight' product (hard filter)",
        p1.hard_constraints_passed,
    )

    # Case 2: "cushioning" must-have must match a product described as
    # "cushioned midsole" - the "-ed" inflection must not defeat the match.
    ev2 = build_evidence(
        query="cushioned running shoes",
        raw_said="I want something with good cushioning",
        products=CATALOG,
        shopping_state={"must_have": ["cushioning"]},
        memory_facts=None,
        catalog_sample=CATALOG,
        platform_unavailable=False,
    )
    p2 = next(p for p in ev2.products if p.product_id == "P2")
    check(
        "cushioning must-have matches a 'cushioned midsole' product (hard filter)",
        p2.hard_constraints_passed,
    )

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all false-decline regression checks passed")
    print()
    return 1 if failures else 0


sys.exit(main())
