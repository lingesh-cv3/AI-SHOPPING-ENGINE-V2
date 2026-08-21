"""Normalized commerce models.

These are the CV3-standard shapes. A platform returning `{"product_id": 123,
"qty": 17}` and one returning `{"sku": "ABC", "available_units": 17}` both
become the same `Product` here. Nothing above the adapter layer ever sees a
platform's native field names.

Money is never a float. Every monetary value is a `Money` carrying a Decimal
amount and an explicit currency, because a payment-recovery system that
silently loses fractions of a rupee is not one anybody should run.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator


class _Base(BaseModel):
    """Shared config: reject unknown fields.

    Strictness here is deliberate. If a platform grows a field and an adapter
    starts passing it through unmapped, we want a loud validation failure at
    the boundary rather than platform-shaped data leaking upstream unnoticed.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)


class Money(_Base):
    """An amount in a specific currency."""

    amount: Decimal
    currency: str = Field(min_length=3, max_length=3, description="ISO 4217, uppercase")

    @field_validator("currency")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    def __str__(self) -> str:
        return f"{self.amount} {self.currency}"


class Availability(StrEnum):
    """Normalized stock state.

    Deliberately coarse. Platforms disagree wildly on what a stock number
    means — some report warehouse totals, some report sellable units, some
    lie. The engine only needs to reason about whether a shopper can buy it.
    """

    IN_STOCK = "IN_STOCK"
    OUT_OF_STOCK = "OUT_OF_STOCK"
    LOW_STOCK = "LOW_STOCK"
    BACKORDER = "BACKORDER"
    PREORDER = "PREORDER"
    UNKNOWN = "UNKNOWN"


class ProductVariant(_Base):
    """A specific buyable configuration of a product — size, colour, and so on."""

    variant_id: str
    sku: str | None = None
    title: str | None = None
    price: Money | None = None
    availability: Availability = Availability.UNKNOWN
    quantity_available: int | None = Field(
        default=None,
        description="None means the platform does not expose a number, which is "
        "different from zero. Never conflate the two.",
    )
    attributes: dict[str, str] = Field(
        default_factory=dict, description="Normalized option values, e.g. {'size': 'M'}"
    )


class Product(_Base):
    """A catalog item."""

    product_id: str
    sku: str | None = None
    title: str
    description: str | None = None
    price: Money | None = None
    compare_at_price: Money | None = None
    availability: Availability = Availability.UNKNOWN
    url: str | None = None
    image_url: str | None = None
    categories: list[str] = Field(default_factory=list)
    variants: list[ProductVariant] = Field(default_factory=list)


class InventoryStatus(_Base):
    """The answer to checkInventory()."""

    product_id: str
    variant_id: str | None = None
    availability: Availability
    quantity_available: int | None = None
    checked_at: datetime


class CartLine(_Base):
    """One line in a shopper's cart."""

    line_id: str
    product_id: str
    variant_id: str | None = None
    title: str
    quantity: int = Field(ge=1)
    unit_price: Money
    line_total: Money


class Cart(_Base):
    """A shopper's cart as the engine understands it."""

    cart_id: str
    lines: list[CartLine] = Field(default_factory=list)
    subtotal: Money
    discount_total: Money | None = None
    tax_total: Money | None = None
    shipping_total: Money | None = None
    grand_total: Money | None = None
    applied_promotions: list[str] = Field(default_factory=list)
    currency: str = Field(min_length=3, max_length=3)

    @property
    def item_count(self) -> int:
        return sum(line.quantity for line in self.lines)

    @property
    def is_empty(self) -> bool:
        return not self.lines


class PromotionResult(_Base):
    """The outcome of applyPromotion()."""

    applied: bool
    code: str
    discount: Money | None = None
    reason: str | None = Field(
        default=None,
        description="Why it was rejected, when applied is False. Human-readable; "
        "the machine-readable version is the ErrorCode raised instead.",
    )


class OrderStatus(StrEnum):
    """Normalized order lifecycle."""

    PENDING = "PENDING"
    AWAITING_PAYMENT = "AWAITING_PAYMENT"
    PAID = "PAID"
    FULFILLED = "FULFILLED"
    CANCELLED = "CANCELLED"
    REFUNDED = "REFUNDED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class PaymentStatus(StrEnum):
    """Normalized payment state, tracked separately from order state.

    An order can be PENDING with payment DECLINED — that combination is
    precisely the recovery case this whole system exists for, so the two must
    not be collapsed into one field.
    """

    NOT_ATTEMPTED = "NOT_ATTEMPTED"
    AUTHORIZED = "AUTHORIZED"
    CAPTURED = "CAPTURED"
    DECLINED = "DECLINED"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    UNKNOWN = "UNKNOWN"


class DeclineReason(StrEnum):
    """Normalized decline causes.

    This is the input to diagnosis. Platforms and gateways express these very
    differently; the adapter maps them here. UNKNOWN is common and honest —
    many gateways deliberately do not say why, and the engine must behave
    sensibly when it cannot know.
    """

    INSUFFICIENT_FUNDS = "INSUFFICIENT_FUNDS"
    CARD_EXPIRED = "CARD_EXPIRED"
    INCORRECT_DETAILS = "INCORRECT_DETAILS"
    LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
    ISSUER_DECLINED = "ISSUER_DECLINED"
    SUSPECTED_FRAUD = "SUSPECTED_FRAUD"
    NETWORK_ERROR = "NETWORK_ERROR"
    UNKNOWN = "UNKNOWN"


class Order(_Base):
    """An order as the engine understands it."""

    order_id: str
    status: OrderStatus
    payment_status: PaymentStatus = PaymentStatus.UNKNOWN
    decline_reason: DeclineReason | None = None
    lines: list[CartLine] = Field(default_factory=list)
    grand_total: Money | None = None
    amount_paid: Money | None = None
    currency: str = Field(min_length=3, max_length=3)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CheckoutResult(_Base):
    """The outcome of checkout()."""

    succeeded: bool
    order: Order | None = None
    payment_status: PaymentStatus = PaymentStatus.UNKNOWN
    decline_reason: DeclineReason | None = None
    redirect_url: str | None = Field(
        default=None,
        description="Some platforms complete payment in a hosted flow rather than "
        "via API. When set, the shopper must be sent here to finish.",
    )


class PaymentRecoveryMethod(StrEnum):
    """Recovery mechanisms the standard interface can express.

    Whether any of these are actually reachable on a given merchant connection
    is entirely a capability question — the enum describes what CV3 can ask
    for, never what a platform can do. Most connections will support none of
    these, and that is a supported outcome, not a failure.
    """

    RETRY_SAME_METHOD = "RETRY_SAME_METHOD"
    ALTERNATE_METHOD = "ALTERNATE_METHOD"
    SPLIT_PAYMENT = "SPLIT_PAYMENT"
    PAYMENT_LINK = "PAYMENT_LINK"


class PaymentRecoveryResult(_Base):
    """The outcome of recoverPayment()."""

    succeeded: bool
    method_used: PaymentRecoveryMethod
    order: Order | None = None
    amount_recovered: Money | None = None
    payment_link: str | None = None
    reason: str | None = None


class ProductSearchResult(_Base):
    """The outcome of searchProducts().

    `total_available` is separate from `len(products)` so the engine can tell
    the difference between "no results exist" and "results exist beyond this
    page" — a distinction that matters, because the first is a dead search
    worth recovering and the second is not.
    """

    query: str
    products: list[Product] = Field(default_factory=list)
    total_available: int | None = None

    @property
    def is_dead_search(self) -> bool:
        """No results at all — the friction signal this system exists to catch."""
        return not self.products