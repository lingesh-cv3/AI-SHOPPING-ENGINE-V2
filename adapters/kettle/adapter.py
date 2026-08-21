"""Kettle & Bloom adapter — the second platform.

The point of this file is what it does NOT change. It implements the same
StandardCommerceInterface as the Northfield adapter, against a platform that shares
none of its conventions: GraphQL instead of REST, one endpoint instead of many, a
data/errors envelope instead of a bare body, proper HTTP status codes instead of
200-with-an-error-key.

Nothing above this class knows any of that. The engine issues checkInventory() and
gets an InventoryStatus, exactly as it does for Northfield.

The consequential difference is the last method. Northfield declares recoverPayment
unsupported and raises. This platform supports it, so the engine's behaviour on a
declined card changes completely - not because the AI reasoned differently, not
because the risk rules differ, but because the merchant's platform can do something
the other one cannot. That is the whole argument for the adapter layer, made
visible.
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

PLATFORM_NAME = "kettle-graphql"

#: How the engine's recovery vocabulary maps onto this platform's. Northfield needs
#: no such table, because it has no recovery at all.
_METHOD = {
    PaymentRecoveryMethod.RETRY_SAME_METHOD: "RETRY_SAME_METHOD",
    PaymentRecoveryMethod.ALTERNATE_METHOD: "ALTERNATE_METHOD",
    PaymentRecoveryMethod.PAYMENT_LINK: "PAYMENT_LINK",
}


class KettleAdapter(StandardCommerceInterface):
    """One merchant connection to a Kettle & Bloom storefront."""

    def __init__(
        self,
        *,
        connection_id: str,
        base_url: str,
        api_key: str | None = None,
        storefront_url: str | None = None,
    ) -> None:
        self.connection_id = connection_id
        self.platform = PLATFORM_NAME
        self.storefront_url = storefront_url

        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = PlatformClient(
            base_url, connection_id=connection_id, headers=headers
        )

    async def close(self) -> None:
        await self._client.close()

    # ---- GraphQL plumbing ------------------------------------------------

    async def _gql(self, operation: str, field: str, **variables: Any) -> Any:
        """Send one GraphQL operation and unwrap the response.

        Every call goes through here, which is this adapter's equivalent of
        Northfield's _unwrap guard. Different convention, same responsibility: no
        platform-shaped error may cross this boundary.
        """
        body = await self._client.request(
            "POST",
            "/graphql",
            json={
                "query": f"{{ {operation} }}",
                "operationName": operation,
                "variables": {k: v for k, v in variables.items() if v is not None},
            },
        )

        if errors := body.get("errors"):
            first = errors[0]
            code = (first.get("extensions") or {}).get("code", "")
            raise CommerceError(
                mapping.error_code_for(code),
                first.get("message") or "the platform reported an error",
                platform_detail=body,
                retryable=False,
            )

        data = body.get("data")
        if not isinstance(data, dict) or field not in data:
            raise CommerceError(
                ErrorCode.UPSTREAM_ERROR,
                f"expected '{field}' in the GraphQL response",
                platform_detail=body,
            )
        return data[field]

    # ---- Capability ------------------------------------------------------

    async def get_capabilities(self) -> CapabilitySet:
        """Declare what this connection supports.

        Read the difference against Northfield: recoverPayment is supported here,
        and three concrete recovery methods are declared. That single difference is
        what makes a declined card recoverable on this merchant and escalation-only
        on the other.
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
            Operation.RECOVER_PAYMENT,
        }
        operations = {
            op: OperationCapability(operation=op, supported=True) for op in supported
        }

        operations[Operation.CHECK_INVENTORY].constraints = {
            # Declared honestly so nothing downstream expects a number. The engine
            # can then say "in stock" rather than "3 left", which is all this
            # merchant actually knows.
            "reports_quantity": False,
            "stock_granularity": "boolean",
        }
        operations[Operation.SEARCH_PRODUCTS].constraints = {
            "search_kind": "keyword_words",
            "semantic_search": False,
            "searches_collection": True,
        }
        operations[Operation.RECOVER_PAYMENT].constraints = {
            # Retrying the identical method fails again, so the engine should not
            # propose it first. Stated rather than left to be discovered.
            "same_method_retry_effective": False,
        }

        return CapabilitySet(
            connection_id=self.connection_id,
            platform=self.platform,
            operations=operations,
            payment_recovery_methods=[
                PaymentRecoveryMethod.ALTERNATE_METHOD,
                PaymentRecoveryMethod.PAYMENT_LINK,
                PaymentRecoveryMethod.RETRY_SAME_METHOD,
            ],
            supports_webhooks=False,
            declared_at=datetime.now(UTC),
        )

    # ---- Catalog ---------------------------------------------------------

    async def search_products(
        self, query: str, *, limit: int = 20, dept: str | None = None
    ) -> ProductSearchResult:
        found = await self._gql(
            "products", "products", search=query or None, collection=dept, limit=limit
        )
        return ProductSearchResult(
            query=query,
            products=[
                mapping.to_product(p, storefront_url=self.storefront_url) for p in found
            ],
            total_available=len(found),
        )

    async def list_departments(self) -> list[str]:
        return await self._gql("collections", "collections")

    async def get_product(self, product_id: str) -> Product:
        raw = await self._gql("product", "product", id=product_id)
        return mapping.to_product(raw, storefront_url=self.storefront_url)

    async def check_inventory(
        self, product_id: str, *, variant_id: str | None = None
    ) -> InventoryStatus:
        raw = await self._gql("product", "product", id=product_id)
        return mapping.to_inventory(raw, option_id=variant_id)

    # ---- Cart ------------------------------------------------------------

    async def create_cart(self) -> Cart:
        return mapping.to_cart(await self._gql("createBag", "createBag"))

    async def get_cart(self, cart_id: str) -> Cart:
        return mapping.to_cart(await self._gql("bag", "bag", id=cart_id))

    async def add_to_cart(
        self,
        cart_id: str,
        product_id: str,
        *,
        variant_id: str | None = None,
        quantity: int = 1,
    ) -> Cart:
        raw = await self._gql(
            "addLine",
            "addLine",
            bagId=cart_id,
            productId=product_id,
            optionId=variant_id,
            quantity=quantity,
        )
        return mapping.to_cart(raw)

    async def update_cart(self, cart_id: str, line_id: str, *, quantity: int) -> Cart:
        raw = await self._gql(
            "updateLine", "updateLine", bagId=cart_id, lineId=line_id, quantity=quantity
        )
        return mapping.to_cart(raw)

    # ---- Orders ----------------------------------------------------------

    async def get_order(self, order_id: str) -> Order:
        return mapping.to_order(await self._gql("order", "order", id=order_id))

    # ---- Money-touching --------------------------------------------------

    async def apply_promotion(self, cart_id: str, code: str) -> PromotionResult:
        raw = await self._gql("applyDiscount", "applyDiscount", bagId=cart_id, code=code)
        cart = mapping.to_cart(raw)
        return PromotionResult(
            applied=True, code=code.upper(), discount=cart.discount_total
        )

    async def checkout(self, cart_id: str, *, idempotency_key: str) -> CheckoutResult:
        return await self.checkout_with_card(
            cart_id, card_last4="1111", idempotency_key=idempotency_key
        )

    async def checkout_with_card(
        self, cart_id: str, *, card_last4: str, idempotency_key: str
    ) -> CheckoutResult:
        """Test-only variant allowing a specific card, to trigger declines.

        Cards ending 0002, 0003 and 0005 fail here - and unlike Northfield's, these
        failures are recoverable.
        """
        raw = await self._gql(
            "placeOrder",
            "placeOrder",
            bagId=cart_id,
            cardLast4=card_last4,
            idempotencyKey=idempotency_key,
        )
        order = mapping.to_order(raw)
        return CheckoutResult(
            succeeded=(raw.get("payment") or {}).get("state") == "SETTLED",
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
        """Attempt to recover a failed payment. Supported on this platform.

        The one method Northfield refuses outright. A shopper whose card declined on
        Northfield can only be escalated to a person; here, the engine can actually
        try something - subject to the same risk gate, because it still moves money
        and therefore still needs a human to approve it.
        """
        platform_method = _METHOD.get(method)
        if platform_method is None:
            raise CapabilityUnsupported(
                Operation.RECOVER_PAYMENT, connection_id=self.connection_id
            )

        raw = await self._gql(
            "retryPayment",
            "retryPayment",
            orderId=order_id,
            method=platform_method,
            idempotencyKey=idempotency_key,
        )
        order = mapping.to_order(raw["order"])
        recovered = bool(raw.get("recovered"))
        return PaymentRecoveryResult(
            succeeded=recovered,
            method_used=method,
            order=order,
            amount_recovered=order.amount_paid if recovered else None,
            reason=raw.get("message"),
        )