"""Kettle & Bloom data store.

Money is handled as Decimal throughout and serialized as a string, because that is
what this platform's API exposes. Northfield used integer paise. Neither is wrong;
they are just different, and the adapter is where the difference stops.

The important behaviour here is payment recovery, which this platform supports.
Northfield has no recovery endpoint at all, so a declined card there can only be
escalated. Here, an alternate payment method or a payment link can actually be
attempted. Same engine, same shopper, different outcome - decided entirely by what
the merchant's platform can do.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from .seed.catalog import CATALOG, COLLECTIONS, DISCOUNT_CODES

_products: dict[str, dict[str, Any]] = {p["id"]: dict(p) for p in CATALOG}
_bags: dict[str, dict[str, Any]] = {}
_orders: dict[str, dict[str, Any]] = {}

_bag_seq = itertools.count(1)
_order_seq = itertools.count(1)
_line_seq = itertools.count(1)

#: Cards that fail on the first attempt. Unlike Northfield's, these are
#: recoverable - which is the whole point of this merchant existing.
DECLINE_CARDS = {
    "0002": "CARD_DECLINED_NSF",
    "0003": "CARD_EXPIRED",
    "0005": "ISSUER_UNAVAILABLE",
    #: Passes the first decline the way every other card does, but the card
    #: itself is hard-blocked: no recovery method will clear it, alternate or
    #: otherwise. The engine therefore proposes a recovery, a person approves
    #: it, and it then fails on the platform - the approved-then-failed outcome
    #: an operator needs to see rendered honestly. A real gateway distinguishes
    #: "this payment failed" from "this card cannot pay", and so do we.
    "0006": "CARD_HARD_BLOCKED",
}

GST = Decimal("0.18")
FREE_DELIVERY_OVER = Decimal("1500.00")
DELIVERY = Decimal("120.00")


def _money(amount: Decimal) -> dict:
    return {"amount": f"{amount:.2f}", "currencyCode": "INR"}


def _now() -> str:
    """ISO 8601, unlike Northfield's epoch strings. Another thing to normalize."""
    return datetime.now(UTC).isoformat()


def reset() -> None:
    _bags.clear()
    _orders.clear()


def collections() -> list[str]:
    return list(COLLECTIONS)


def search(
    term: str | None, collection: str | None, limit: int, offset: int = 0
) -> tuple[list[dict], int]:
    """Word-based search over name and collection.

    Slightly better than Northfield's, because it also searches the collection - so
    "espresso" finds the espresso blend and "gifts" finds the gift boxes. Still no
    synonyms: "beans" finds nothing, because no name contains it.

    `offset` pages through a catalogue larger than one page, mirroring the same
    bounded-pagination capability Northfield's REST endpoint has - a real
    GraphQL commerce API (Shopify's, Shopware's) offers this as `first`/`after`
    or `limit`/`offset`. Returns the page alongside the total match count, so a
    caller can tell "no more results" from "more results exist beyond this page".
    """
    items = list(_products.values())
    if collection:
        items = [p for p in items if p["collection"].lower() == collection.lower()]

    words = [w for w in (term or "").lower().split() if len(w) > 2]
    if words:
        scored = []
        for product in items:
            haystack = f"{product['name']} {product['collection']}".lower()
            hits = sum(1 for w in words if w in haystack or w.rstrip("s") in haystack)
            if hits:
                scored.append((hits, product))
        scored.sort(key=lambda pair: -pair[0])
        items = [p for _, p in scored]

    total = len(items)
    return items[offset : offset + limit], total


# ---------------------------------------------------------------------------
# Test-only bulk seeding
# ---------------------------------------------------------------------------
#
# Same reasoning as Northfield's: a >100-product catalogue exercised without
# permanently growing the real demo catalogue. Prefixed TESTBULK- so it can be
# added and removed cleanly.

_BULK_PREFIX = "TESTBULK-"


def seed_bulk(count: int) -> int:
    """Add `count` synthetic products. Deterministic by index: every 2nd is
    out of stock, the rest in stock - this platform has no LOW_STOCK concept
    at all (see mapping.availability's own docstring), so none are seeded."""
    added = 0
    for i in range(count):
        pid = f"{_BULK_PREFIX}{i:05d}"
        in_stock = (i % 2) != 0
        _products[pid] = {
            "id": pid,
            "name": f"Synthetic Bulk Item {i:05d}",
            "collection": "Bulk-Test",
            "price": {"amount": "1000.00", "currencyCode": "INR"},
            "inStock": in_stock,
            "options": [],
            "story": "synthetic test fixture product, not a real catalogue item",
        }
        added += 1
    return added


def clear_bulk() -> int:
    """Remove every synthetic bulk product added by seed_bulk. Idempotent."""
    ids = [pid for pid in _products if pid.startswith(_BULK_PREFIX)]
    for pid in ids:
        del _products[pid]
    return len(ids)


def product(product_id: str) -> dict | None:
    return _products.get(product_id)


def create_bag() -> dict:
    ref = f"BAG-{next(_bag_seq):04d}"
    _bags[ref] = {"id": ref, "lines": [], "discountCode": None, "createdAt": _now()}
    return price_bag(ref)


def bag(bag_ref: str) -> dict | None:
    return price_bag(bag_ref) if bag_ref in _bags else None


def add_line(bag_ref: str, product_id: str, option_id: str | None, qty: int):
    """Add a line, or return an error code string."""
    if bag_ref not in _bags:
        return "BAG_NOT_FOUND"
    item = _products.get(product_id)
    if item is None:
        return "PRODUCT_NOT_FOUND"

    if option_id:
        option = next((o for o in item["options"] if o["id"] == option_id), None)
        if option is None:
            return "OPTION_NOT_FOUND"
        if not option["inStock"]:
            return "OUT_OF_STOCK"
    elif not item["inStock"]:
        return "OUT_OF_STOCK"

    for line in _bags[bag_ref]["lines"]:
        if line["productId"] == product_id and line["optionId"] == option_id:
            line["quantity"] += qty
            return price_bag(bag_ref)

    _bags[bag_ref]["lines"].append(
        {
            "id": f"LINE-{next(_line_seq):04d}",
            "productId": product_id,
            "optionId": option_id,
            "name": item["name"],
            "quantity": qty,
            "unitPrice": dict(item["price"]),
        }
    )
    return price_bag(bag_ref)


def update_line(bag_ref: str, line_id: str, qty: int):
    if bag_ref not in _bags:
        return "BAG_NOT_FOUND"
    lines = _bags[bag_ref]["lines"]
    for i, line in enumerate(lines):
        if line["id"] == line_id:
            if qty <= 0:
                lines.pop(i)
            else:
                line["quantity"] = qty
            return price_bag(bag_ref)
    return "LINE_NOT_FOUND"


def apply_discount(bag_ref: str, code: str):
    if bag_ref not in _bags:
        return "BAG_NOT_FOUND"
    rule = DISCOUNT_CODES.get(code.upper())
    if rule is None:
        return "DISCOUNT_NOT_FOUND"
    if not rule["active"]:
        return "DISCOUNT_INACTIVE"

    goods = sum(
        Decimal(line["unitPrice"]["amount"]) * line["quantity"]
        for line in _bags[bag_ref]["lines"]
    )
    if goods < Decimal(rule["minSpend"]):
        return "DISCOUNT_MIN_SPEND"

    _bags[bag_ref]["discountCode"] = code.upper()
    return price_bag(bag_ref)


def price_bag(bag_ref: str) -> dict:
    b = _bags[bag_ref]
    goods = sum(
        Decimal(line["unitPrice"]["amount"]) * line["quantity"] for line in b["lines"]
    ) or Decimal("0.00")

    discount = Decimal("0.00")
    if code := b.get("discountCode"):
        rule = DISCOUNT_CODES[code]
        discount = (
            (goods * Decimal(rule["value"]) / 100)
            if rule["kind"] == "PERCENT"
            else min(Decimal(rule["value"]), goods)
        )

    net = goods - discount
    tax = (net * GST).quantize(Decimal("0.01"))
    delivery = Decimal("0.00") if (net >= FREE_DELIVERY_OVER or net == 0) else DELIVERY

    return {
        **b,
        "subtotal": _money(goods),
        "discount": _money(discount),
        "tax": _money(tax),
        "delivery": _money(delivery),
        "total": _money(net + tax + delivery),
    }


def place_order(bag_ref: str, card_last4: str, idempotency_key: str):
    """Place an order. Replaying an idempotency key returns the original."""
    for existing in _orders.values():
        if existing["idempotencyKey"] == idempotency_key:
            return existing

    if bag_ref not in _bags:
        return "BAG_NOT_FOUND"
    priced = price_bag(bag_ref)
    if not priced["lines"]:
        return "BAG_EMPTY"

    ref = f"KB-{next(_order_seq):04d}"
    failure = DECLINE_CARDS.get(card_last4)

    _orders[ref] = {
        "id": ref,
        "bagId": bag_ref,
        "idempotencyKey": idempotency_key,
        "lines": [dict(line) for line in priced["lines"]],
        "total": priced["total"],
        "paid": _money(Decimal("0.00")) if failure else priced["total"],
        "state": "PENDING_PAYMENT" if failure else "CONFIRMED",
        "payment": {
            "state": "FAILED" if failure else "SETTLED",
            "failureCode": failure,
            "attempts": 1,
        },
        "placedAt": _now(),
    }
    return _orders[ref]


def order(order_id: str) -> dict | None:
    return _orders.get(order_id)


#: What this platform can actually attempt. Northfield exposes none of these.
RECOVERY_METHODS = ("ALTERNATE_METHOD", "PAYMENT_LINK", "RETRY_SAME_METHOD")


#: A decline reason no recovery can clear. The card is the problem, not the
#: payment method, so an alternate method or a payment link on the same card
#: fails exactly as the identical retry does.
HARD_BLOCKED = "CARD_HARD_BLOCKED"


def retry_payment(order_id: str, method: str, idempotency_key: str):
    """Attempt to recover a failed payment.

    The behaviour is deliberately realistic rather than convenient: retrying the
    same method fails again, because the bank's answer has not changed. Switching
    method or sending a payment link succeeds - except for a hard-blocked card,
    where no method helps because the card itself, not the payment, is refused.
    An engine that could only retry identically would be no use; a gateway that
    never distinguished a dead card from a bad method would be no use either.
    """
    o = _orders.get(order_id)
    if o is None:
        return "ORDER_NOT_FOUND"
    if o["payment"]["state"] == "SETTLED":
        return "ALREADY_PAID"
    if method not in RECOVERY_METHODS:
        return "METHOD_NOT_SUPPORTED"
    if o["payment"].get("failureCode") == HARD_BLOCKED:
        return {
            "order": o,
            "recovered": False,
            "method": method,
            "message": "this card is blocked - no recovery method will clear it",
        }

    # Idempotent: the same key twice does not charge twice.
    if o.get("recoveryKey") == idempotency_key:
        return {
            "order": o,
            "recovered": o["payment"]["state"] == "SETTLED",
            "method": method,
            "message": "already attempted with this key",
        }

    o["recoveryKey"] = idempotency_key
    o["payment"]["attempts"] += 1

    if method == "RETRY_SAME_METHOD":
        return {
            "order": o,
            "recovered": False,
            "method": method,
            "message": "the bank declined again for the same reason",
        }

    o["state"] = "CONFIRMED"
    o["payment"] = {
        "state": "SETTLED",
        "failureCode": None,
        "attempts": o["payment"]["attempts"],
    }
    o["paid"] = dict(o["total"])
    return {
        "order": o,
        "recovered": True,
        "method": method,
        "message": (
            "paid via an alternate method"
            if method == "ALTERNATE_METHOD"
            else "a payment link was sent and settled"
        ),
    }