"""Shared fixtures for the basket-budget tests (not a test itself: the runner only collects test_*.py).

`FakeShop` is a controllable platform adapter: prices, currency, stock, cart contents, failures and call counts are all under the
test's control, and ANY write method (add/create/checkout/update/remove/apply...) raises, so a test that finishes proves the
guided/budget code only read. It imports nothing from `engine` (the importing test sets DATABASE_URL first).
"""
from __future__ import annotations

import re
from collections import Counter
from decimal import Decimal

from shared.models import Availability, Cart, CartLine, Money, Product, ProductSearchResult


def product(pid, title, price, *, currency="INR", stock=True, compatible_with=None, categories=None):
    return Product(
        product_id=pid, title=title, description=title,
        price=None if price is None else Money(amount=Decimal(str(price)), currency=currency),
        availability=Availability.IN_STOCK if stock else Availability.OUT_OF_STOCK,
        categories=categories or [], compatible_with=compatible_with or [],
    )


def standard_catalogue(currency="INR"):
    """Shoes 1999/4999/8999, socks 499/799, energy 899/1799 (so lows sum to 3397, typicals to 6997)."""
    return [
        product("S-A", "Alpha Running Shoes", 1999, currency=currency),
        product("S-B", "Bravo Running Shoes", 4999, currency=currency),
        product("S-C", "Charlie Running Shoes", 8999, currency=currency),
        product("K-1", "Crew Socks", 499, currency=currency),
        product("K-2", "Trail Socks", 799, currency=currency),
        product("E-1", "Energy Gel Box", 1799, currency=currency),
        product("E-2", "Energy Chew Bar", 899, currency=currency),
    ]


class FakeShop:
    def __init__(self, products, *, currency="INR"):
        self.products = {p.product_id: p for p in products}
        self.currency = currency
        self.cart_lines: list[tuple[str, int]] = []
        self.calls = Counter()
        self.cart_fails = False
        self.search_fails = False
        self.product_fails: set[str] = set()
        #: every write the code under test ATTEMPTED (recorded before raising, because callers may swallow the exception)
        self.write_attempts: list[str] = []

    # ---- reads
    async def search_products(self, query, limit=20, **kw):
        self.calls["search_products"] += 1
        if self.search_fails:
            raise RuntimeError("platform down")
        stems = [w.rstrip("s") for w in re.findall(r"[a-z]+", (query or "").lower()) if len(w) > 2]
        hits = [p for p in self.products.values() if any(s in p.title.lower() for s in stems)]
        return ProductSearchResult(query=query, products=hits[:limit], total_available=len(hits))

    async def get_product(self, product_id):
        self.calls["get_product"] += 1
        if product_id in self.product_fails or product_id not in self.products:
            raise RuntimeError(f"cannot fetch {product_id}")
        return self.products[product_id]

    async def get_cart(self, cart_id):
        self.calls["get_cart"] += 1
        if self.cart_fails:
            raise RuntimeError("cart unreadable")
        lines, total = [], Decimal(0)
        for i, (pid, qty) in enumerate(self.cart_lines):
            p = self.products[pid]
            line_total = p.price.amount * qty
            total += line_total
            lines.append(CartLine(line_id=f"L{i}", product_id=pid, title=p.title, quantity=qty,
                                  unit_price=p.price, line_total=Money(amount=line_total, currency=p.price.currency)))
        return Cart(cart_id=cart_id, lines=lines, subtotal=Money(amount=total, currency=self.currency),
                    grand_total=Money(amount=total, currency=self.currency), currency=self.currency)

    # ---- test helper (NOT an adapter method the code under test may call)
    def put_in_cart(self, product_id, qty=1):
        self.cart_lines.append((product_id, qty))

    # ---- any write the code under test attempts is a test failure
    def __getattr__(self, name):
        if name.startswith(("add", "create", "checkout", "update", "remove", "apply", "set", "delete", "pay", "clear")):
            def _forbidden(*a, **kw):
                self.write_attempts.append(name)
                raise AssertionError(f"the budget/guided code attempted a write: adapter.{name}")
            return _forbidden
        raise AttributeError(name)


async def start_mission(shop, message, *, sid=None, conn="conn_demo", cart_id="C1"):
    """Parse and start a guided mission against `shop`; returns `(session_id, spec, turn)`. Imports engine lazily so the calling test
    can set DATABASE_URL first."""
    import uuid

    from engine.shopper_v2 import guided_mission as g
    from engine.shopper_v2.state import ShoppingStateV2

    sid = sid or f"bud_{uuid.uuid4().hex[:8]}"
    state = ShoppingStateV2(session_id=sid, connection_id=conn)
    spec = g.parse_mission(message)
    turn = await g.start(adapter=shop, spec=spec, message=message, state=state, cart_id=cart_id, memory_facts=None) if spec else None
    return sid, spec, turn


def stored_total(row):
    """The mission's shared total as stored on its guided stages (None when absent). The session-wide `budget_amount` column is NOT
    where it lives: that field has other readers (a per-product cap in mission ranking, the cart budget-reduction suggestion)."""
    from decimal import Decimal

    for d in (row or {}).get("mission_items") or []:
        if d.get("guided") and d.get("total_budget") not in (None, ""):
            return Decimal(str(d["total_budget"]))
    return None
