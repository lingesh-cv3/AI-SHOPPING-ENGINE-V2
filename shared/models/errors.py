"""Normalized errors.

Every platform reports failure differently. Platform A says PRODUCT_NOT_FOUND,
Platform B says ITEM_UNAVAILABLE, Platform C returns a bare 404. The adapter's
job is to collapse all of that into one vocabulary before anything upstream
sees it, so the AI Engine, Decision Engine and Risk Gate never learn a single
platform-specific error string.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    """The complete set of failures the engine understands.

    An adapter may not invent codes outside this enum. If a platform produces
    a failure that maps to none of these, it normalizes to UPSTREAM_ERROR with
    the native detail preserved in `platform_detail` for logs.
    """

    # Catalog and inventory
    PRODUCT_UNAVAILABLE = "PRODUCT_UNAVAILABLE"
    INVENTORY_INSUFFICIENT = "INVENTORY_INSUFFICIENT"
    VARIANT_UNAVAILABLE = "VARIANT_UNAVAILABLE"

    # Cart and checkout
    CART_NOT_FOUND = "CART_NOT_FOUND"
    CART_INVALID = "CART_INVALID"
    CHECKOUT_FAILED = "CHECKOUT_FAILED"
    CHECKOUT_LOCKED = "CHECKOUT_LOCKED"

    # Promotions
    PROMOTION_INELIGIBLE = "PROMOTION_INELIGIBLE"
    PROMOTION_EXPIRED = "PROMOTION_EXPIRED"
    PROMOTION_NOT_FOUND = "PROMOTION_NOT_FOUND"

    # Orders and payment
    ORDER_NOT_FOUND = "ORDER_NOT_FOUND"
    PAYMENT_DECLINED = "PAYMENT_DECLINED"
    PAYMENT_RECOVERY_FAILED = "PAYMENT_RECOVERY_FAILED"

    # Connection and capability
    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    AUTH_FAILED = "AUTH_FAILED"
    RATE_LIMITED = "RATE_LIMITED"
    TIMEOUT = "TIMEOUT"
    UPSTREAM_ERROR = "UPSTREAM_ERROR"

    # Engine-side
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    VALIDATION_ERROR = "VALIDATION_ERROR"


#: Codes where retrying the identical request may succeed. Everything else is
#: terminal — a declined card does not become approved by asking again.
RETRYABLE_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.RATE_LIMITED,
        ErrorCode.TIMEOUT,
        ErrorCode.UPSTREAM_ERROR,
    }
)


class CommerceError(Exception):
    """The only exception type that may cross an adapter boundary.

    Adapters catch whatever their platform throws and re-raise as this. Upstream
    code matches on `.code`, never on strings or platform exception classes.

    `platform_detail` carries the raw platform payload for operators reading
    logs. It is deliberately never surfaced to the AI Engine or the shopper —
    it is the one field allowed to contain platform-specific shape, and it
    exists so that normalization does not destroy debuggability.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        platform_detail: Any = None,
        retryable: bool | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.platform_detail = platform_detail
        # An adapter may override retryability when it knows better than the
        # default — e.g. a platform that returns a generic upstream error for
        # a permanently rejected order.
        self.retryable = RETRYABLE_CODES.__contains__(code) if retryable is None else retryable
        super().__init__(f"{code}: {message}")

    def __repr__(self) -> str:
        return (
            f"CommerceError(code={self.code!r}, message={self.message!r}, "
            f"retryable={self.retryable!r})"
        )


class CapabilityUnsupported(CommerceError):
    """Raised when an operation is not available on this merchant connection.

    This is a distinct class because it is not really a failure — it is the
    system working correctly. The formal rule from the architecture is:
    unsupported capability -> no execution attempt -> policy-based fallback or
    escalation. Reaching this exception means something upstream skipped its
    capability check, so it is worth being able to catch it specifically.
    """

    def __init__(self, operation: str, *, connection_id: str | None = None) -> None:
        detail = f"operation '{operation}' is not supported"
        if connection_id:
            detail += f" on connection '{connection_id}'"
        super().__init__(
            ErrorCode.CAPABILITY_UNSUPPORTED,
            detail,
            retryable=False,
        )
        self.operation = operation
        self.connection_id = connection_id
