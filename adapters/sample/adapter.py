"""Reference platform adapter for the sample merchant.

This is the first concrete implementation of StandardCommerceInterface, and it
exists to prove the contract holds before any real client depends on it. If the
step-1 contract was wrong, it fails here.

Two things in this adapter matter more than the rest.

**The _unwrap guard.** This platform returns business errors as HTTP 200 with an
{"error": "CODE"} body. Every response goes through _unwrap, which checks for
that key and raises a typed CommerceError. An adapter that only inspected status
codes would hand the engine a malformed object and the failure would surface
somewhere far away and much later.

**recover_payment raises CapabilityUnsupported.** The platform has no such
endpoint, so the adapter says so plainly rather than improvising.
get_capabilities declares it unsupported and payment_recovery_methods is empty.
This is not a degraded state - it is the honest answer, and it is what most real
merchant connections will report.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from shared.interfaces import StandardCommerceInterface
from shared.models import (
    CapabilitySet,
    CapabilityUnsupported,
    Cart,
    CheckoutResult,
    CommerceError,
    ErrorCode,
    InventoryStatus,
    Money,
    Operation,
    OperationCapability,
    Order,
    PaymentRecoveryMethod,
    PaymentRecoveryResult,
    Product,
    ProductSearchResult,
    PromotionResult,
)

from ..framework.http import PlatformClient
from . import mapping

API = "/api/v1"

PLATFORM_NAME = "sample-merchant"


class SampleMerchantAdapter(StandardCommerceInterface):
    """One merchant connection to a sample-merchant backend.

    The class is the reusable platform adapter; an instance is one merchant
    connection. Two merchants on this platform construct two instances with
    different base URLs, credentials and currencies, and share no state.
    """

    def __init__(
        self,
        *,
        connection_id: str,
        base_url: str,
        currency: str = "INR",
        api_key: str | None = None,
        storefront_url: str | None = None,
    ) -> None:
        self.connection_id = connection_id
        self.platform = PLATFORM_NAME
        # The platform states no currency anywhere, so it is configuration.
        # Guessing per-response would be worse than requiring it here.
        self.currency = currency.upper()
        self.storefront_url = storefront_url

        headers = {"X-Api-Key": api_key} if api_key else {}
        self._client = PlatformClient(
            base_url, connection_id=connection_id, headers=headers
        )

    async def close(self) -> None:
        await self._client.close()

    # ---- The HTTP-200-error guard ---------------------------------------

    def _unwrap(self, body: dict[str, Any], key: str) -> Any:
        """Extract a payload, raising on the platform's in-body error convention.

        Called on every single response. This is the one line of defence against
        a platform that signals failure with HTTP 200, and skipping it anywhere
        would let a malformed object reach the engine.
        """
        if "error" in body:
            platform_error = body["error"]
            raise CommerceError(
                mapping.error_code_for(platform_error),
                f"platform reported {platform_error}",
                platform_detail=body,
                retryable=False,
            )
        if key not in body:
            raise CommerceError(
                ErrorCode.UPSTREAM_ERROR,
                f"expected '{key}' in response",
                platform_detail=body,
            )
        return body[key]

    # ---- Capability ------------------------------------------------------

    async def get_capabilities(self) -> CapabilitySet:
        """Declare what this connection can actually do.

        Hardcoded for this platform because its capabilities are fixed - it is a
        known backend with a known API surface. An adapter for a platform where
        capabilities vary per store (feature flags, plan tiers) would probe the
        platform instead. Both are valid; what matters is that the answer is
        true.
        """
        supported = {
            Operation.GET_CAPABILITIES,
            Operation.SEARCH_PRODUCTS,
            Operation.GET_PRODUCT,
            Operation.CHECK_INVENTORY,
            Operation.GET_CART,
            Operation.ADD_TO_CART,
            Operation.UPDATE_CART,
            Operation.GET_ORDER,
            Operation.APPLY_PROMOTION,
            Operation.CHECKOUT,
        }

        operations: dict[Operation, OperationCapability] = {
            op: OperationCapability(operation=op, supported=True) for op in supported
        }

        # The honest declaration. There is no recovery endpoint on this
        # platform, so nothing downstream may attempt one.
        operations[Operation.RECOVER_PAYMENT] = OperationCapability(
            operation=Operation.RECOVER_PAYMENT,
            supported=False,
            reason="platform exposes no payment-recovery or refund endpoint",
        )

        operations[Operation.SEARCH_PRODUCTS].constraints = {
            # Keyword substring matching only. Worth declaring, because it tells
            # the engine that a dead search may well be a search-quality problem
            # rather than a genuinely empty catalog.
            "search_kind": "keyword_substring",
            "semantic_search": False,
        }
        operations[Operation.CHECKOUT].constraints = {
            "modifiable_after_creation": False,
            "idempotency_supported": True,
        }

        return CapabilitySet(
            connection_id=self.connection_id,
            platform=self.platform,
            operations=operations,
            payment_recovery_methods=[],  # none, and that is the truth
            supports_webhooks=False,
            declared_at=datetime.now(UTC),
        )

    # ---- Catalog ---------------------------------------------------------

    async def search_products(
        self, query: str, *, limit: int = 20, dept: str | None = None
    ) -> ProductSearchResult:
        """Search or browse. An empty result is returned, never raised.

        `dept` is an extension beyond the standard interface. Category browsing is
        not on the Standard Commerce Interface because not every platform has a
        comparable concept - some have collections, some have tags, some have
        nothing. Where a platform does support it, the adapter can expose it, and
        the Decision Engine simply never uses it.
        """
        params: dict[str, object] = {"q": query, "limit": limit}
        if dept:
            params["dept"] = dept

        body = await self._client.request("GET", f"{API}/items", params=params)
        items = self._unwrap(body, "items")
        return ProductSearchResult(
            query=query,
            products=[
                mapping.to_product(
                    item, currency=self.currency, storefront_url=self.storefront_url
                )
                for item in items
            ],
            total_available=body.get("match_count"),
        )

    async def list_departments(self) -> list[str]:
        """Platform-specific extension, not part of the standard interface."""
        body = await self._client.request("GET", f"{API}/departments")
        return self._unwrap(body, "departments")

    async def get_product(self, product_id: str) -> Product:
        body = await self._client.request("GET", f"{API}/items/{product_id}")
        item = self._unwrap(body, "item")
        return mapping.to_product(
            item, currency=self.currency, storefront_url=self.storefront_url
        )

    async def check_inventory(
        self, product_id: str, *, variant_id: str | None = None
    ) -> InventoryStatus:
        params = {"variant_ref": variant_id} if variant_id else None
        body = await self._client.request(
            "GET", f"{API}/stock/{product_id}", params=params
        )
        return mapping.to_inventory(self._unwrap(body, "stock"))

    # ---- Cart ------------------------------------------------------------

    async def create_cart(self) -> Cart:
        """Not on the standard interface - the engine never creates carts.

        A shopper's cart already exists by the time the engine is involved. This
        is here for tests and demos only.
        """
        body = await self._client.request("POST", f"{API}/basket")
        return mapping.to_cart(self._unwrap(body, "basket"), currency=self.currency)

    async def get_cart(self, cart_id: str) -> Cart:
        body = await self._client.request("GET", f"{API}/basket/{cart_id}")
        return mapping.to_cart(self._unwrap(body, "basket"), currency=self.currency)

    async def add_to_cart(
        self,
        cart_id: str,
        product_id: str,
        *,
        variant_id: str | None = None,
        quantity: int = 1,
    ) -> Cart:
        body = await self._client.request(
            "POST",
            f"{API}/basket/{cart_id}/lines",
            json={"product_id": product_id, "variant_ref": variant_id, "qty": quantity},
        )
        return mapping.to_cart(self._unwrap(body, "basket"), currency=self.currency)

    async def update_cart(self, cart_id: str, line_id: str, *, quantity: int) -> Cart:
        body = await self._client.request(
            "PATCH", f"{API}/basket/{cart_id}/lines/{line_id}", json={"qty": quantity}
        )
        return mapping.to_cart(self._unwrap(body, "basket"), currency=self.currency)

    # ---- Orders ----------------------------------------------------------

    async def get_order(self, order_id: str) -> Order:
        body = await self._client.request("GET", f"{API}/purchase/{order_id}")
        return mapping.to_order(self._unwrap(body, "purchase"), currency=self.currency)

    # ---- Money-touching --------------------------------------------------
    # Reached only after Decision -> Risk -> Approval has cleared. The adapter
    # never calls these on its own initiative.

    async def apply_promotion(self, cart_id: str, code: str) -> PromotionResult:
        """Apply a voucher.

        An ineligible or expired voucher raises rather than returning
        applied=False, because the platform reports it as an error and the engine
        needs the specific code (PROMOTION_EXPIRED versus PROMOTION_INELIGIBLE)
        to diagnose and choose a fallback.
        """
        body = await self._client.request(
            "POST", f"{API}/basket/{cart_id}/voucher", json={"code": code}
        )
        basket = self._unwrap(body, "basket")
        cart = mapping.to_cart(basket, currency=self.currency)
        return PromotionResult(
            applied=True, code=code.upper(), discount=cart.discount_total
        )

    async def checkout(self, cart_id: str, *, idempotency_key: str) -> CheckoutResult:
        """Complete the order.

        A declined payment is a *successful call* returning succeeded=False - the
        order exists, it is simply unpaid. That distinction is the entire basis
        of the recovery flow, so it must not be collapsed into an exception.
        """
        body = await self._client.request(
            "POST",
            f"{API}/purchase",
            json={
                "basket_ref": cart_id,
                # Test card. Real card handling never passes through the engine;
                # a production adapter would reference a payment token held by
                # the merchant's own checkout.
                "card_last4": "1111",
                "idem_key": idempotency_key,
            },
            idempotency_key=idempotency_key,
        )
        purchase = self._unwrap(body, "purchase")
        order = mapping.to_order(purchase, currency=self.currency)
        return CheckoutResult(
            succeeded=purchase.get("pay_state") == "TAKEN",
            order=order,
            payment_status=order.payment_status,
            decline_reason=order.decline_reason,
        )

    async def checkout_with_card(
        self, cart_id: str, *, card_last4: str, idempotency_key: str
    ) -> CheckoutResult:
        """Test-only variant allowing a specific card, to trigger declines.

        Not on the standard interface. Exists so demos and tests can force a
        decline on demand - cards ending 0002, 0003 and 0004 always fail.
        """
        body = await self._client.request(
            "POST",
            f"{API}/purchase",
            json={
                "basket_ref": cart_id,
                "card_last4": card_last4,
                "idem_key": idempotency_key,
            },
            idempotency_key=idempotency_key,
        )
        purchase = self._unwrap(body, "purchase")
        order = mapping.to_order(purchase, currency=self.currency)
        return CheckoutResult(
            succeeded=purchase.get("pay_state") == "TAKEN",
            order=order,
            payment_status=order.payment_status,
            decline_reason=order.decline_reason,
        )

    async def recover_payment(
        self,
        order_id: str,
        *,
        method: PaymentRecoveryMethod,
        idempotency_key: str,
        amount: Money | None = None,
    ) -> PaymentRecoveryResult:
        """Unsupported on this platform.

        No endpoint exists, so the adapter refuses rather than improvising. The
        engine's correct response is to escalate to a human, per the formal rule:
        unsupported capability -> no execution attempt -> policy-based fallback
        or escalation.

        This is the single most important behaviour in the adapter. Every real
        merchant will have capabilities they lack, and the system has to stay
        correct when they do.
        """
        raise CapabilityUnsupported(
            Operation.RECOVER_PAYMENT, connection_id=self.connection_id
        )