"""Translation: Kettle & Bloom shape -> CV3 normalized models.

The same job as the Northfield mapper, against a platform that agrees with it on
almost nothing. Reading the two side by side is the clearest statement of what the
adapter layer is for.

The consequential difference: this platform has no stock counts. It says inStock
true or false and nothing more. So every InventoryStatus produced here has
quantity_available of None - not zero. The contract drew that distinction
deliberately, and Northfield never exercised it because it always gives a number.
This is the platform that proves the distinction was worth having.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
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

#: This platform's error codes, in its own vocabulary, mapped to ours. Compare with
#: Northfield's ERROR_MAP: entirely different strings, identical destinations.
ERROR_MAP: dict[str, ErrorCode] = {
    "PRODUCT_NOT_FOUND": ErrorCode.PRODUCT_UNAVAILABLE,
    "OPTION_NOT_FOUND": ErrorCode.VARIANT_UNAVAILABLE,
    "OUT_OF_STOCK": ErrorCode.INVENTORY_INSUFFICIENT,
    "BAG_NOT_FOUND": ErrorCode.CART_NOT_FOUND,
    "BAG_EMPTY": ErrorCode.CART_INVALID,
    "LINE_NOT_FOUND": ErrorCode.CART_INVALID,
    "DISCOUNT_NOT_FOUND": ErrorCode.PROMOTION_NOT_FOUND,
    "DISCOUNT_INACTIVE": ErrorCode.PROMOTION_EXPIRED,
    "DISCOUNT_MIN_SPEND": ErrorCode.PROMOTION_INELIGIBLE,
    "ORDER_NOT_FOUND": ErrorCode.ORDER_NOT_FOUND,
    "ALREADY_PAID": ErrorCode.PAYMENT_RECOVERY_FAILED,
    "METHOD_NOT_SUPPORTED": ErrorCode.CAPABILITY_UNSUPPORTED,
}

DECLINE_MAP: dict[str, DeclineReason] = {
    "CARD_DECLINED_NSF": DeclineReason.INSUFFICIENT_FUNDS,
    "CARD_EXPIRED": DeclineReason.CARD_EXPIRED,
    "ISSUER_UNAVAILABLE": DeclineReason.ISSUER_DECLINED,
}


def error_code_for(platform_error: str) -> ErrorCode:
    return ERROR_MAP.get(platform_error, ErrorCode.UPSTREAM_ERROR)


def decline_reason_for(code: str | None) -> DeclineReason | None:
    if code is None:
        return None
    return DECLINE_MAP.get(code, DeclineReason.UNKNOWN)


def money(node: dict[str, Any] | None) -> Money | None:
    """A {amount, currencyCode} object -> Money.

    The currency comes from the platform here, unlike Northfield where it had to be
    configured. Better, and the adapter absorbs the difference so nothing upstream
    knows which platform states its currency and which does not.

    A malformed amount returns None rather than raising or defaulting to zero.
    Silently treating an unparseable price as free is how a shop sells things by
    accident.
    """
    if not node or node.get("amount") is None:
        return None
    try:
        amount = Decimal(str(node["amount"])).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return Money(amount=amount, currency=node.get("currencyCode") or "INR")


def iso_to_datetime(value: str | None) -> datetime | None:
    """ISO 8601 -> datetime. Northfield sent epoch seconds; this sends ISO."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def availability(in_stock: bool | None) -> Availability:
    """Boolean -> Availability.

    Note what is NOT here: no LOW_STOCK. This platform cannot express it, so we
    never claim it. Inventing a threshold from a boolean would be fabricating
    information the merchant did not give us.
    """
    if in_stock is None:
        return Availability.UNKNOWN
    return Availability.IN_STOCK if in_stock else Availability.OUT_OF_STOCK


def to_variant(raw: dict[str, Any], *, price: dict | None) -> ProductVariant:
    return ProductVariant(
        variant_id=raw["id"],
        title=raw.get("label"),
        price=money(price),
        availability=availability(raw.get("inStock")),
        # None, not zero. This platform does not count, and pretending otherwise
        # would tell the engine something the merchant never said.
        quantity_available=None,
        attributes={"format": raw["label"]} if raw.get("label") else {},
    )


def to_product(raw: dict[str, Any], *, storefront_url: str | None = None) -> Product:
    return Product(
        product_id=raw["id"],
        title=raw["name"],
        description=raw.get("story"),
        price=money(raw.get("price")),
        compare_at_price=None,
        availability=availability(raw.get("inStock")),
        url=f"{storefront_url}/product/{raw['id']}" if storefront_url else None,
        categories=[raw["collection"]] if raw.get("collection") else [],
        variants=[
            to_variant(o, price=raw.get("price")) for o in raw.get("options", [])
        ],
    )


def to_inventory(raw: dict[str, Any], *, option_id: str | None = None) -> InventoryStatus:
    if option_id:
        option = next((o for o in raw.get("options", []) if o["id"] == option_id), None)
        in_stock = option["inStock"] if option else None
    else:
        in_stock = raw.get("inStock")

    return InventoryStatus(
        product_id=raw["id"],
        variant_id=option_id,
        availability=availability(in_stock),
        quantity_available=None,
        checked_at=datetime.now(UTC),
    )


def to_cart_line(raw: dict[str, Any], currency: str) -> CartLine:
    unit = money(raw["unitPrice"])
    qty = raw["quantity"]
    return CartLine(
        line_id=raw["id"],
        product_id=raw["productId"],
        variant_id=raw.get("optionId"),
        title=raw["name"],
        quantity=qty,
        unit_price=unit,
        line_total=Money(amount=unit.amount * qty, currency=unit.currency),
    )


def to_cart(raw: dict[str, Any]) -> Cart:
    """A bag -> Cart. Vocabulary shift again: bag/lines/discountCode."""
    currency = (raw.get("total") or {}).get("currencyCode") or "INR"
    discount = money(raw.get("discount"))
    code = raw.get("discountCode")
    return Cart(
        cart_id=raw["id"],
        lines=[to_cart_line(line, currency) for line in raw.get("lines", [])],
        subtotal=money(raw.get("subtotal")) or Money(amount=Decimal("0.00"), currency=currency),
        discount_total=discount if discount and discount.amount > 0 else None,
        tax_total=money(raw.get("tax")),
        shipping_total=money(raw.get("delivery")),
        grand_total=money(raw.get("total")),
        applied_promotions=[code] if code else [],
        currency=currency,
    )


def order_status(state: str | None, payment_state: str | None) -> OrderStatus:
    match state:
        case "CONFIRMED":
            return OrderStatus.PAID if payment_state == "SETTLED" else OrderStatus.PENDING
        case "PENDING_PAYMENT":
            return OrderStatus.AWAITING_PAYMENT
        case "CANCELLED":
            return OrderStatus.CANCELLED
        case _:
            return OrderStatus.UNKNOWN


def payment_status(state: str | None) -> PaymentStatus:
    match state:
        case "SETTLED":
            return PaymentStatus.CAPTURED
        case "FAILED":
            return PaymentStatus.DECLINED
        case "AUTHORIZED":
            return PaymentStatus.AUTHORIZED
        case _:
            return PaymentStatus.UNKNOWN


def to_order(raw: dict[str, Any]) -> Order:
    payment = raw.get("payment") or {}
    currency = (raw.get("total") or {}).get("currencyCode") or "INR"
    return Order(
        order_id=raw["id"],
        status=order_status(raw.get("state"), payment.get("state")),
        payment_status=payment_status(payment.get("state")),
        decline_reason=decline_reason_for(payment.get("failureCode")),
        lines=[to_cart_line(line, currency) for line in raw.get("lines", [])],
        grand_total=money(raw.get("total")),
        amount_paid=money(raw.get("paid")),
        currency=currency,
        created_at=iso_to_datetime(raw.get("placedAt")),
        updated_at=iso_to_datetime(raw.get("placedAt")),
    )