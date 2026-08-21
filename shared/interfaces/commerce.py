"""The Standard Commerce Interface.

The stable contract the engine issues actions against, regardless of platform.
An adapter subclasses this and implements every method. The engine imports
only this class and the normalized models — never a concrete adapter.

Two rules govern implementations:

1. Never leak platform shape upward. Every return value is a normalized model;
   every failure is a CommerceError with a standard code. A caller must not be
   able to tell which platform answered.

2. Never make a business decision. The adapter translates and executes. It does
   not decide whether an action should happen, does not classify risk, and does
   not hold conversation state. Those belong to the Decision Engine, the Risk
   Gate, and the session service respectively.

Unsupported operations raise CapabilityUnsupported rather than returning an
empty result, so a missing capability can never be mistaken for a real answer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models.capability import CapabilitySet
from ..models.commerce import (
    Cart,
    CheckoutResult,
    InventoryStatus,
    Money,
    Order,
    PaymentRecoveryMethod,
    PaymentRecoveryResult,
    Product,
    ProductSearchResult,
    PromotionResult,
)


class StandardCommerceInterface(ABC):
    """One merchant connection's view of a commerce backend.

    An instance is bound to a single merchant connection — its credentials,
    its configuration, its capability set. The reusable platform adapter is the
    class; a connection is an instance. Two merchants on the same platform
    construct two instances of the same class and share no state.
    """

    #: Set by the adapter framework at construction. Used for logging, error
    #: context, and scoping every persisted record to one tenant.
    connection_id: str
    platform: str

    # ---- Capability -----------------------------------------------------

    @abstractmethod
    async def get_capabilities(self) -> CapabilitySet:
        """Declare what this connection actually supports.

        Called when a connection is established and whenever its configuration
        changes. The result is what every capability check upstream tests
        against. An adapter must report honestly — declaring an operation it
        cannot perform produces a runtime failure inside an approved action,
        which is the worst place to discover it.
        """

    # ---- Catalog (read-only) --------------------------------------------

    @abstractmethod
    async def search_products(self, query: str, *, limit: int = 20) -> ProductSearchResult:
        """Find products matching a query.

        Returning zero products is a valid, expected result — it is the dead
        search this system exists to recover. Do not raise on no results.
        """

    @abstractmethod
    async def get_product(self, product_id: str) -> Product:
        """Retrieve one product. Raises PRODUCT_UNAVAILABLE if it does not exist."""

    @abstractmethod
    async def check_inventory(
        self, product_id: str, *, variant_id: str | None = None
    ) -> InventoryStatus:
        """Check stock.

        Where a platform exposes no quantity, return the availability enum with
        quantity_available left as None. None and zero mean different things and
        must not be conflated.
        """

    # ---- Cart -----------------------------------------------------------

    @abstractmethod
    async def get_cart(self, cart_id: str) -> Cart:
        """Read a shopper's cart."""

    @abstractmethod
    async def add_to_cart(
        self,
        cart_id: str,
        product_id: str,
        *,
        variant_id: str | None = None,
        quantity: int = 1,
    ) -> Cart:
        """Add a line and return the updated cart."""

    @abstractmethod
    async def update_cart(self, cart_id: str, line_id: str, *, quantity: int) -> Cart:
        """Change a line's quantity. Quantity zero removes the line."""

    # ---- Orders ---------------------------------------------------------

    @abstractmethod
    async def get_order(self, order_id: str) -> Order:
        """Retrieve order status and detail."""

    # ---- Money-touching -------------------------------------------------
    # Everything below travels the full Decision -> Risk -> Approval path
    # before an adapter method is ever called. An adapter must never invoke
    # these on its own initiative.

    @abstractmethod
    async def apply_promotion(self, cart_id: str, code: str) -> PromotionResult:
        """Apply a coupon or promotion to a cart."""

    @abstractmethod
    async def checkout(self, cart_id: str, *, idempotency_key: str) -> CheckoutResult:
        """Complete the order.

        The idempotency key is required, not optional. A retry after a timeout
        must not create a second order.
        """

    @abstractmethod
    async def recover_payment(
        self,
        order_id: str,
        *,
        method: PaymentRecoveryMethod,
        idempotency_key: str,
        amount: Money | None = None,
    ) -> PaymentRecoveryResult:
        """Execute an approved payment-recovery mechanism.

        Read this as "recovery via whatever validated mechanism this merchant's
        architecture actually exposes" — never as a promise that CV3 can
        recover payments in general. Most connections will declare this
        unsupported and must raise CapabilityUnsupported.
        """


class SupportsWebhooks(ABC):
    """Optional mixin for platforms that emit events.

    Kept separate from the main interface because webhook support is genuinely
    optional — an adapter for a platform without events should not be forced to
    implement stubs that raise.
    """

    @abstractmethod
    async def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify a webhook's authenticity before any processing.

        Called before parsing. An unverified webhook is discarded, not parsed
        and then rejected.
        """

    @abstractmethod
    async def parse_webhook(self, headers: dict[str, str], body: bytes) -> list:
        """Translate a verified webhook into normalized Signal objects.

        Returns a list because one platform event can carry several signals,
        and an event carrying none is valid — return an empty list rather than
        raising.
        """