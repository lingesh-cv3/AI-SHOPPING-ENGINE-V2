"""Sample merchant data store.

In-memory on purpose. This is a fake shop whose job is to give the adapter a
real HTTP backend to talk to; persistence is not what we are testing, and a
restart giving a clean slate is convenient during development. Everything goes
through this module, so swapping to SQLite later is contained to one file.

Two things here exist specifically to make demos work:

- Payment declines are *controllable*. A card ending 0002 always declines with
  insufficient funds. Without a reliable way to trigger a decline, the recovery
  path cannot be demonstrated on request.
- Search is a literal substring match. It is bad on purpose — that badness is
  the dead-search friction the engine recovers from.
"""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

from .seed.catalog import CATALOG, DEPARTMENTS, VOUCHERS

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

_products: dict[str, dict[str, Any]] = {p["product_id"]: dict(p) for p in CATALOG}
_baskets: dict[str, dict[str, Any]] = {}
_purchases: dict[str, dict[str, Any]] = {}

_basket_seq = itertools.count(1)
_purchase_seq = itertools.count(1)
_line_seq = itertools.count(1)

#: Cards that always fail, so a decline can be triggered on demand.
#: Keyed by last four digits.
DECLINE_CARDS: dict[str, str] = {
    "0002": "INSUFFICIENT_BALANCE",
    "0003": "CARD_EXPIRED",
    "0004": "BANK_REFUSED",
}


def _now() -> str:
    """Timestamps as epoch seconds in a string.

    Deliberately not ISO 8601. Plenty of real platforms do this, and the
    adapter should be the only place that has to know.
    """
    return str(int(datetime.now(UTC).timestamp()))


def reset() -> None:
    """Clear transactional state. Used by tests and demo resets."""
    _baskets.clear()
    _purchases.clear()


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def list_departments() -> list[str]:
    """Every department in the catalog."""
    return list(DEPARTMENTS)


def search_items(
    query: str, limit: int = 20, dept: str | None = None
) -> tuple[list[dict], int]:
    """Browse or search.

    Search matches on *words*, not on the raw string. "running shoes" finds the
    Trailblazer Running Shoe because "running" and "shoe" both appear in the title,
    even though the exact phrase does not.

    This is still deliberately unsophisticated - no stemming, no synonyms, no
    understanding. "trainers" finds nothing because this shop says shoes, and
    "waterproof" finds nothing because that word is in the description rather than
    the title. That gap between what a shopper means and what a keyword index holds
    is exactly the friction the engine recovers from, and it is what most real
    storefront search actually does.

    Results are ranked by how many query words matched, so a two-word match
    outranks a one-word match rather than arriving in catalog order.
    """
    items = list(_products.values())
    if dept:
        items = [p for p in items if p["dept"].lower() == dept.lower()]

    words = [w for w in query.lower().split() if len(w) > 2]
    if words:
        scored = []
        for product in items:
            title = product["item_title"].lower()
            # Singular and plural both count. Crude, but a shopper typing "shoes"
            # should not be punished for it.
            hits = sum(1 for w in words if w in title or w.rstrip("s") in title)
            if hits:
                scored.append((hits, product))
        scored.sort(key=lambda pair: -pair[0])
        items = [product for _, product in scored]

    return items[:limit], len(items)

def get_item(product_id: str) -> dict | None:
    return _products.get(product_id)


def get_stock(product_id: str, variant_ref: str | None = None) -> dict | None:
    product = _products.get(product_id)
    if not product:
        return None
    if variant_ref:
        for v in product.get("variants", []):
            if v["variant_ref"] == variant_ref:
                return {
                    "product_id": product_id,
                    "variant_ref": variant_ref,
                    "qty_available": v["qty_available"],
                    "stock_state": "Y" if v["qty_available"] > 0 else "N",
                }
        return None
    return {
        "product_id": product_id,
        "variant_ref": None,
        "qty_available": product["qty_available"],
        "stock_state": product["stock_state"],
    }


# ---------------------------------------------------------------------------
# Baskets
# ---------------------------------------------------------------------------


def create_basket() -> dict:
    ref = f"BSK{next(_basket_seq):05d}"
    basket = {
        "basket_ref": ref,
        "lines": [],
        "voucher_code": None,
        "voucher_discount_paise": 0,
        "created_ts": _now(),
    }
    _baskets[ref] = basket
    return _price_basket(basket)


def get_basket(basket_ref: str) -> dict | None:
    basket = _baskets.get(basket_ref)
    return _price_basket(basket) if basket else None


def add_line(
    basket_ref: str, product_id: str, variant_ref: str | None, qty: int
) -> dict | str:
    """Add a line. Returns the basket, or an error string on failure.

    Returning a bare string for errors is intentionally sloppy — see the API
    module for why. The adapter is responsible for turning it into a proper
    typed error.
    """
    basket = _baskets.get(basket_ref)
    if basket is None:
        return "BASKET_MISSING"
    product = _products.get(product_id)
    if product is None:
        return "ITEM_NOT_FOUND"

    stock = get_stock(product_id, variant_ref)
    if stock is None:
        return "VARIANT_NOT_FOUND"
    if stock["qty_available"] < qty:
        return "NOT_ENOUGH_STOCK"

    # Merge with an existing identical line rather than duplicating it.
    for line in basket["lines"]:
        if line["product_id"] == product_id and line["variant_ref"] == variant_ref:
            line["qty"] += qty
            return _price_basket(basket)

    basket["lines"].append(
        {
            "line_ref": f"LN{next(_line_seq):05d}",
            "product_id": product_id,
            "variant_ref": variant_ref,
            "item_title": product["item_title"],
            "qty": qty,
            "unit_price_paise": product["price_paise"],
        }
    )
    return _price_basket(basket)


def update_line(basket_ref: str, line_ref: str, qty: int) -> dict | str:
    basket = _baskets.get(basket_ref)
    if basket is None:
        return "BASKET_MISSING"
    for i, line in enumerate(basket["lines"]):
        if line["line_ref"] == line_ref:
            if qty <= 0:
                basket["lines"].pop(i)
            else:
                stock = get_stock(line["product_id"], line["variant_ref"])
                if stock and stock["qty_available"] < qty:
                    return "NOT_ENOUGH_STOCK"
                line["qty"] = qty
            return _price_basket(basket)
    return "LINE_NOT_FOUND"


def apply_voucher(basket_ref: str, code: str) -> dict | str:
    basket = _baskets.get(basket_ref)
    if basket is None:
        return "BASKET_MISSING"

    voucher = VOUCHERS.get(code.upper())
    if voucher is None:
        return "VOUCHER_UNKNOWN"
    if not voucher["live"]:
        return "VOUCHER_DEAD"

    goods = sum(line["unit_price_paise"] * line["qty"] for line in basket["lines"])
    if goods < voucher["min_spend_paise"]:
        return "VOUCHER_MIN_SPEND"

    basket["voucher_code"] = code.upper()
    return _price_basket(basket)


def _price_basket(basket: dict) -> dict:
    """Recompute totals. Called on every read, so totals are never stale."""
    goods = sum(line["unit_price_paise"] * line["qty"] for line in basket["lines"])

    discount = 0
    code = basket.get("voucher_code")
    if code and (voucher := VOUCHERS.get(code)):
        if voucher["kind"] == "PCT":
            discount = goods * voucher["value"] // 100
        else:
            discount = min(voucher["value"], goods)
    basket["voucher_discount_paise"] = discount

    net = goods - discount
    # Flat 18% GST on the discounted amount, and free shipping over 2000 rupees.
    tax = net * 18 // 100
    delivery = 0 if net >= 200000 or net == 0 else 9900

    return {
        **basket,
        "goods_total_paise": goods,
        "tax_paise": tax,
        "delivery_paise": delivery,
        "payable_paise": net + tax + delivery,
    }


# ---------------------------------------------------------------------------
# Purchases
# ---------------------------------------------------------------------------


def create_purchase(basket_ref: str, card_last4: str, idem_key: str) -> dict | str:
    """Attempt a purchase.

    Idempotency is honoured: replaying the same key returns the original
    purchase rather than creating a second one. A real platform that got this
    wrong would double-charge on a retry, so the adapter must be able to rely
    on it.
    """
    for purchase in _purchases.values():
        if purchase["idem_key"] == idem_key:
            return purchase

    basket = _baskets.get(basket_ref)
    if basket is None:
        return "BASKET_MISSING"
    priced = _price_basket(basket)
    if not priced["lines"]:
        return "BASKET_EMPTY"

    ref = f"ORD{next(_purchase_seq):05d}"
    decline = DECLINE_CARDS.get(card_last4)

    purchase = {
        "purchase_ref": ref,
        "basket_ref": basket_ref,
        "idem_key": idem_key,
        "lines": [dict(line) for line in priced["lines"]],
        "payable_paise": priced["payable_paise"],
        "paid_paise": 0 if decline else priced["payable_paise"],
        "state": "UNPAID" if decline else "CONFIRMED",
        "pay_state": "REFUSED" if decline else "TAKEN",
        "refusal_code": decline,
        "created_ts": _now(),
    }
    _purchases[ref] = purchase
    return purchase


def get_purchase(purchase_ref: str) -> dict | None:
    return _purchases.get(purchase_ref)


def list_purchases() -> list[dict]:
    return list(_purchases.values())