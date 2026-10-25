"""Superlatives stay honest: no 'cheapest' across currencies, and a sold-out pick says so.

Before: `pick_superlative` compared raw amounts, so 900 JPY beat 10 USD as "cheapest", and a sold-out cheapest product was announced with no mention of stock.
Pure functions plus the two reply builders - no services needed.

    python tests/shopper_v2/test_superlative_honesty.py
"""

from __future__ import annotations

import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_superlative.db")
assert "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine.execution import ranking  # noqa: E402
from engine.shopper_v2 import compare, graph  # noqa: E402
from engine.shopper_v2.reference_resolution import parse_query_intent  # noqa: E402
from shared.models import Availability, Money, Product  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail=None) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + ("" if ok or detail is None else f"   {detail!r}"))
    if not ok:
        failures += 1


def p(pid, amount, currency="USD", availability=Availability.IN_STOCK, rating=None):
    return Product(product_id=pid, title=f"Item {pid}", price=Money(amount=amount, currency=currency), availability=availability, rating=rating)


print("1. Mixed currencies are never ranked against each other")
mixed = [p("A", 10, "USD"), p("B", 900, "JPY")]
check("no cheapest across USD and JPY", ranking.pick_superlative(mixed, "price", "min") is None)
check("no most expensive across USD and JPY", ranking.pick_superlative(mixed, "price", "max") is None)
check("a sort by price leaves the order alone", [x.product_id for x in ranking.sort_products([mixed[1], mixed[0]], "price", "asc")] == ["B", "A"])
check("rating still ranks across currencies (it is currency-free)", ranking.pick_superlative([p("A", 10, "USD", rating=4.0), p("B", 900, "JPY", rating=4.5)], "rating", "max").product_id == "B")
same = [p("A", 10), p("B", 7), p("C", 12)]
check("one currency still ranks normally", ranking.pick_superlative(same, "price", "min").product_id == "B")
check("a product with no price does not make it 'mixed'", ranking.pick_superlative([p("A", 10), Product(product_id="N", title="No price")], "price", "min").product_id == "A")

print("2. A sold-out pick is disclosed, an in-stock pick is not annotated")
sold_out = p("S", 5, availability=Availability.OUT_OF_STOCK)
intent = parse_query_intent("which is the cheapest")
check("the intent parses as a price minimum", intent.superlative == "min" and intent.sort_attribute == "price", (intent.superlative, intent.sort_attribute))
check("graph reply says 'out of stock'", "out of stock" in graph._superlative_reply(sold_out, intent), graph._superlative_reply(sold_out, intent))
check("compare reply says 'out of stock'", "out of stock" in (compare._deterministic_superlative("which is the cheapest", [sold_out, p("T", 9)]) or ""))
check("an in-stock pick has no stock note", "out of stock" not in graph._superlative_reply(p("I", 5), intent))
check("compare with mixed currencies gives no superlative claim", compare._deterministic_superlative("which is the cheapest", mixed) is None)

print("3. The model-free comparison shows ratings when the platform has them, and never invents one")
line = compare._deterministic_comparison([p("A", 10, rating=4.5), p("B", 12)])
check("a rated product shows its rating", "Item A" in line and "rated 4.5" in line, line)
check("an unrated product shows no rating", "Item B" in line and line.count("rated") == 1, line)

print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
sys.exit(1 if failures else 0)
