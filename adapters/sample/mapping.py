"""Translation: sample-merchant shape -> CV3 normalized models.

Deliberately separated from the adapter itself. These are pure functions with no
HTTP, no state and no I/O, so the mapping can be tested exhaustively without a
running server. Mapping bugs are the most common adapter defect and the easiest
to catch this way.

Every function here exists because the platform and the contract disagree about
something: money is integer paise, stock is "Y"/"N"/"LOW" *and* a count, errors
are strings in a body that arrives with HTTP 200, timestamps are epoch-second
strings, and names differ throughout.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from shared.models import (
    Availability,
    Cart,
    CartLine,
    DeclineReason,
    ErrorCode,
    InventoryStatus,
    Money,
    Order,
    OrderStatus,
    PaymentStatus,
    Product,
    ProductVariant,
)

# ---------------------------------------------------------------------------
# Error translation
# ---------------------------------------------------------------------------

#: The platform's error strings, mapped to the engine's vocabulary. Anything not
#: listed becomes UPSTREAM_ERROR rather than being guessed at.
ERROR_MAP: dict[str, ErrorCode] = {
    "ITEM_NOT_FOUND": ErrorCode.PRODUCT_UNAVAILABLE,
    "VARIANT_NOT_FOUND": ErrorCode.VARIANT_UNAVAILABLE,
    "NOT_ENOUGH_STOCK": ErrorCode.INVENTORY_INSUFFICIENT,
    "BASKET_MISSING": ErrorCode.CART_NOT_FOUND,
    "BASKET_EMPTY": ErrorCode.CART_INVALID,
    "LINE_NOT_FOUND": ErrorCode.CART_INVALID,
    "VOUCHER_UNKNOWN": ErrorCode.PROMOTION_NOT_FOUND,
    "VOUCHER_DEAD": ErrorCode.PROMOTION_EXPIRED,
    "VOUCHER_MIN_SPEND": ErrorCode.PROMOTION_INELIGIBLE,
    "PURCHASE_NOT_FOUND": ErrorCode.ORDER_NOT_FOUND,
}

#: The platform's refusal codes, mapped to normalized decline reasons. This is
#: the input to diagnosis, so an unmapped code must become UNKNOWN rather than
#: something plausible - a wrong diagnosis is worse than an honest absence.
DECLINE_MAP: dict[str, DeclineReason] = {
    "INSUFFICIENT_BALANCE": DeclineReason.INSUFFICIENT_FUNDS,
    "CARD_EXPIRED": DeclineReason.CARD_EXPIRED,
    "BANK_REFUSED": DeclineReason.ISSUER_DECLINED,
}


def error_code_for(platform_error: str) -> ErrorCode:
    return ERROR_MAP.get(platform_error, ErrorCode.UPSTREAM_ERROR)


def decline_reason_for(refusal_code: str | None) -> DeclineReason | None:
    if refusal_code is None:
        return None
    return DECLINE_MAP.get(refusal_code, DeclineReason.UNKNOWN)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------


def money(paise: int | None, currency: str) -> Money | None:
    """Integer paise -> Decimal Money.

    The currency comes from connection configuration, not from the platform -
    this platform never states a currency anywhere in its responses. That is a
    real and common gap, and guessing would be worse than requiring it to be
    configured.

    Division is done in Decimal, never float. Decimal(429900) / 100 is exactly
    4299.00; 429900 / 100 in float arithmetic is not reliably exact, and a
    payment system built on approximate money is not one to run.
    """
    if paise is None:
        return None
    return Money(
        amount=(Decimal(paise) / Decimal(100)).quantize(Decimal("0.01")),
        currency=currency,
    )


def epoch_to_datetime(ts: str | int | None) -> datetime | None:
    """Epoch-second string -> aware datetime. Bad values become None, not now()."""
    if ts is None:
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=UTC)
    except (ValueError, TypeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def availability(stock_state: str | None, qty: int | None) -> Availability:
    """Collapse the platform's two overlapping stock signals into one.

    The platform reports both a tri-state flag and a count, and they can
    disagree - a variant with qty_available 0 is unbuyable regardless of what
    the parent product's flag says. The count wins when it is present and zero,
    because a count is a harder fact than a flag.
    """
    if qty is not None and qty <= 0:
        return Availability.OUT_OF_STOCK

    match stock_state:
        case "Y":
            return Availability.IN_STOCK
        case "N":
            return Availability.OUT_OF_STOCK
        case "LOW":
            return Availability.LOW_STOCK
        case _:
            # An unrecognized flag with a positive count is still buyable, but
            # we do not pretend to know more than that.
            return Availability.IN_STOCK if qty else Availability.UNKNOWN


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def to_variant(
    raw: dict[str, Any], *, parent_price: int | None, currency: str
) -> ProductVariant:
    """Map one variant.

    Variants carry no price on this platform, so they inherit the parent's. An
    adapter for a platform with per-variant pricing would read it directly; the
    contract accommodates both.
    """
    qty = raw.get("qty_available")
    return ProductVariant(
        variant_id=raw["variant_ref"],
        title=raw.get("opt_size"),
        price=money(parent_price, currency),
        availability=availability(None, qty),
        quantity_available=qty,
        attributes={"size": raw["opt_size"]} if raw.get("opt_size") else {},
    )


def to_product(
    raw: dict[str, Any], *, currency: str, storefront_url: str | None = None
) -> Product:
    """Map one catalog item."""
    price_paise = raw.get("price_paise")
    return Product(
        product_id=raw["product_id"],
        title=raw["item_title"],
        description=raw.get("blurb"),
        price=money(price_paise, currency),
        compare_at_price=money(raw.get("was_price_paise"), currency),
        availability=availability(raw.get("stock_state"), raw.get("qty_available")),
        url=f"{storefront_url}/product/{raw['product_id']}" if storefront_url else None,
        categories=[raw["dept"]] if raw.get("dept") else [],
        variants=[
            to_variant(v, parent_price=price_paise, currency=currency)
            for v in raw.get("variants", [])
        ],
    )


def to_inventory(raw: dict[str, Any]) -> InventoryStatus:
    qty = raw.get("qty_available")
    return InventoryStatus(
        product_id=raw["product_id"],
        variant_id=raw.get("variant_ref"),
        availability=availability(raw.get("stock_state"), qty),
        quantity_available=qty,
        checked_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Cart
# ---------------------------------------------------------------------------


def to_cart_line(raw: dict[str, Any], *, currency: str) -> CartLine:
    """Map a basket line.

    line_total is computed here - the platform does not send one. Deriving it in
    the adapter means nothing upstream has to know that, and every Cart the
    engine sees is complete.
    """
    unit_paise = raw["unit_price_paise"]
    qty = raw["qty"]
    return CartLine(
        line_id=raw["line_ref"],
        product_id=raw["product_id"],
        variant_id=raw.get("variant_ref"),
        title=raw["item_title"],
        quantity=qty,
        unit_price=money(unit_paise, currency),
        line_total=money(unit_paise * qty, currency),
    )


def to_cart(raw: dict[str, Any], *, currency: str) -> Cart:
    """Map a basket to a Cart.

    Note the vocabulary shift and the field-by-field remap:
    basket_ref -> cart_id, goods_total_paise -> subtotal,
    voucher_discount_paise -> discount_total, delivery_paise -> shipping_total,
    payable_paise -> grand_total, voucher_code -> applied_promotions.
    """
    discount = raw.get("voucher_discount_paise") or 0
    code = raw.get("voucher_code")
    return Cart(
        cart_id=raw["basket_ref"],
        lines=[to_cart_line(line, currency=currency) for line in raw.get("lines", [])],
        subtotal=money(raw.get("goods_total_paise", 0), currency),
        discount_total=money(discount, currency) if discount else None,
        tax_total=money(raw.get("tax_paise"), currency),
        shipping_total=money(raw.get("delivery_paise"), currency),
        grand_total=money(raw.get("payable_paise"), currency),
        applied_promotions=[code] if code else [],
        currency=currency,
    )


# ---------------------------------------------------------------------------
# Order
# ---------------------------------------------------------------------------


def order_status(state: str | None, pay_state: str | None) -> OrderStatus:
    """Map the platform's order state.

    Order state and payment state are tracked separately on purpose - an order
    that exists but is unpaid is the recovery case this whole system is for, and
    collapsing the two would erase it.
    """
    match state:
        case "CONFIRMED":
            return OrderStatus.PAID if pay_state == "TAKEN" else OrderStatus.PENDING
        case "UNPAID":
            return OrderStatus.AWAITING_PAYMENT
        case "CANCELLED":
            return OrderStatus.CANCELLED
        case _:
            return OrderStatus.UNKNOWN


def payment_status(pay_state: str | None) -> PaymentStatus:
    match pay_state:
        case "TAKEN":
            return PaymentStatus.CAPTURED
        case "REFUSED":
            return PaymentStatus.DECLINED
        case "PENDING":
            return PaymentStatus.NOT_ATTEMPTED
        case _:
            return PaymentStatus.UNKNOWN


def to_order(raw: dict[str, Any], *, currency: str) -> Order:
    """Map a purchase to an Order."""
    return Order(
        order_id=raw["purchase_ref"],
        status=order_status(raw.get("state"), raw.get("pay_state")),
        payment_status=payment_status(raw.get("pay_state")),
        decline_reason=decline_reason_for(raw.get("refusal_code")),
        lines=[to_cart_line(line, currency=currency) for line in raw.get("lines", [])],
        grand_total=money(raw.get("payable_paise"), currency),
        amount_paid=money(raw.get("paid_paise"), currency),
        currency=currency,
        created_at=epoch_to_datetime(raw.get("created_ts")),
        updated_at=epoch_to_datetime(raw.get("created_ts")),
    )