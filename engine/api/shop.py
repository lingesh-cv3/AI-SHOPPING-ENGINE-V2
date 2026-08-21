"""Shop routes - commerce operations, routed through the adapter.

The storefront could call the sample merchant on 8001 directly. It deliberately
does not. Everything goes through the adapter, which means the storefront never
sees price_paise or stock_state "Y" - it sees 4299.00 INR and IN_STOCK.

That is the demonstration. A second storefront on a completely different platform
would receive byte-identical responses from these endpoints, because the
normalization happens below this layer.

Money is serialized as a string, never a float. JSON has no decimal type, and
converting to float here would reintroduce exactly the imprecision the Money model
exists to prevent.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from shared.models import CapabilityUnsupported, CommerceError, Money

from .deps import engine

router = APIRouter(prefix="/api/shop", tags=["shop"])


def _money(m: Money | None) -> dict | None:
    """Serialize money without ever going through float."""
    if m is None:
        return None
    return {"amount": str(m.amount), "currency": m.currency, "display": str(m)}


def _adapter(connection_id: str):
    adapter = engine.registry.adapter_for(connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{connection_id}'")
    return adapter


def _handle(exc: CommerceError) -> HTTPException:
    """Turn a normalized commerce error into an HTTP response.

    Note the status codes: a genuine 4xx or 5xx, not the platform's habit of
    returning 200 with an error body. The engine's own API is well behaved even
    though the platform beneath it is not.
    """
    if isinstance(exc, CapabilityUnsupported):
        status = 501  # not implemented - the platform genuinely cannot
    elif str(exc.code).endswith("_UNAVAILABLE") or str(exc.code).endswith("_NOT_FOUND"):
        status = 404
    else:
        status = 400
    return HTTPException(
        status,
        detail={
            "code": str(exc.code),
            "message": exc.message,
            "retryable": exc.retryable,
        },
    )


def _product(p) -> dict:
    return {
        "product_id": p.product_id,
        "title": p.title,
        "description": p.description,
        "price": _money(p.price),
        "compare_at_price": _money(p.compare_at_price),
        "availability": str(p.availability),
        "categories": p.categories,
        "variants": [
            {
                "variant_id": v.variant_id,
                "title": v.title,
                "availability": str(v.availability),
                "quantity_available": v.quantity_available,
            }
            for v in p.variants
        ],
    }


def _cart(c) -> dict:
    return {
        "cart_id": c.cart_id,
        "item_count": c.item_count,
        "is_empty": c.is_empty,
        "currency": c.currency,
        "subtotal": _money(c.subtotal),
        "discount_total": _money(c.discount_total),
        "tax_total": _money(c.tax_total),
        "shipping_total": _money(c.shipping_total),
        "grand_total": _money(c.grand_total),
        "applied_promotions": c.applied_promotions,
        "lines": [
            {
                "line_id": ln.line_id,
                "product_id": ln.product_id,
                "variant_id": ln.variant_id,
                "title": ln.title,
                "quantity": ln.quantity,
                "unit_price": _money(ln.unit_price),
                "line_total": _money(ln.line_total),
            }
            for ln in c.lines
        ],
    }


class AddLine(BaseModel):
    product_id: str
    variant_id: str | None = None
    quantity: int = 1


class PromoBody(BaseModel):
    code: str


class CheckoutBody(BaseModel):
    #: Test cards ending 0002, 0003 and 0004 always decline, so the recovery flow
    #: can be demonstrated on demand rather than waited for.
    card_last4: str = "1111"

@router.get("/{connection_id}/departments")
async def departments(connection_id: str) -> dict:
    """Category list, where the platform supports the concept.

    Not on the Standard Commerce Interface, so this checks for the method rather
    than assuming it. A platform without categories returns an empty list and the
    storefront simply shows no category navigation.
    """
    adapter = _adapter(connection_id)
    lister = getattr(adapter, "list_departments", None)
    if lister is None:
        return {"departments": []}
    try:
        return {"departments": await lister()}
    except CommerceError as exc:
        raise _handle(exc) from exc



def _order(o) -> dict:
    """Serialize an order for the storefront.

    Payment status is kept separate from order status rather than merged into one
    label. An order can exist, be confirmed, and still be unpaid - that
    combination is the recovery case this whole system is for, and collapsing it
    into a single "failed" would erase the thing worth acting on.
    """
    return {
        "order_id": o.order_id,
        "status": str(o.status),
        "payment_status": str(o.payment_status),
        "decline_reason": str(o.decline_reason) if o.decline_reason else None,
        "currency": o.currency,
        "grand_total": _money(o.grand_total),
        "amount_paid": _money(o.amount_paid),
        "created_at": o.created_at.isoformat() if o.created_at else None,
        "lines": [
            {
                "line_id": ln.line_id,
                "product_id": ln.product_id,
                "variant_id": ln.variant_id,
                "title": ln.title,
                "quantity": ln.quantity,
                "unit_price": _money(ln.unit_price),
                "line_total": _money(ln.line_total),
            }
            for ln in o.lines
        ],
    }


@router.get("/{connection_id}/order/{order_id}")
async def get_order(connection_id: str, order_id: str) -> dict:
    """Look up an order.

    Works for paid and unpaid orders alike. A shopper whose card was declined
    still has an order, and being able to look it up is part of what makes the
    sale recoverable rather than lost.
    """
    adapter = _adapter(connection_id)
    try:
        return _order(await adapter.get_order(order_id))
    except CommerceError as exc:
        raise _handle(exc) from exc

@router.get("/{connection_id}/search")
async def search(
    connection_id: str, q: str = "", limit: int = 24, dept: str | None = None
) -> dict:
    """Search or browse the catalog.

    Zero results from a *search* is the dead-search signal. Zero results from a
    *browse* is an empty category, which is not friction - so is_dead_search is
    only meaningful when a query was actually given.
    """
    adapter = _adapter(connection_id)
    try:
        result = await adapter.search_products(q, limit=limit, dept=dept)
    except TypeError:
        # An adapter whose search does not accept dept. Fall back rather than
        # fail, so a platform without categories still works.
        result = await adapter.search_products(q, limit=limit)
    except CommerceError as exc:
        raise _handle(exc) from exc
    return {
        "query": result.query,
        "is_dead_search": result.is_dead_search and bool(q.strip()),
        "total_available": result.total_available,
        "products": [_product(p) for p in result.products],
    }

@router.get("/{connection_id}/product/{product_id}")
async def product(connection_id: str, product_id: str) -> dict:
    adapter = _adapter(connection_id)
    try:
        return _product(await adapter.get_product(product_id))
    except CommerceError as exc:
        raise _handle(exc) from exc


@router.post("/{connection_id}/cart")
async def create_cart(connection_id: str) -> dict:
    adapter = _adapter(connection_id)
    try:
        return _cart(await adapter.create_cart())
    except CommerceError as exc:
        raise _handle(exc) from exc


@router.get("/{connection_id}/cart/{cart_id}")
async def get_cart(connection_id: str, cart_id: str) -> dict:
    adapter = _adapter(connection_id)
    try:
        return _cart(await adapter.get_cart(cart_id))
    except CommerceError as exc:
        raise _handle(exc) from exc


@router.post("/{connection_id}/cart/{cart_id}/lines")
async def add_line(connection_id: str, cart_id: str, body: AddLine) -> dict:
    adapter = _adapter(connection_id)
    try:
        cart = await adapter.add_to_cart(
            cart_id,
            body.product_id,
            variant_id=body.variant_id,
            quantity=body.quantity,
        )
    except CommerceError as exc:
        raise _handle(exc) from exc
    return _cart(cart)


@router.post("/{connection_id}/cart/{cart_id}/promotion")
async def apply_promotion(connection_id: str, cart_id: str, body: PromoBody) -> dict:
    """Apply a coupon.

    WELCOME10 works. SUMMER25 is expired, which is what drives the
    PROMOTION_FAILED friction path.
    """
    adapter = _adapter(connection_id)
    try:
        await adapter.apply_promotion(cart_id, body.code)
        return _cart(await adapter.get_cart(cart_id))
    except CommerceError as exc:
        raise _handle(exc) from exc


@router.post("/{connection_id}/cart/{cart_id}/checkout")
async def checkout(connection_id: str, cart_id: str, body: CheckoutBody) -> dict:
    """Complete the order.

    A decline returns HTTP 200 with succeeded false and a real order. It is not an
    error - it is an unpaid order, which is precisely the thing worth recovering.
    """
    adapter = _adapter(connection_id)
    key = f"idem-{uuid.uuid4().hex[:12]}"
    try:
        result = await adapter.checkout_with_card(
            cart_id, card_last4=body.card_last4, idempotency_key=key
        )
    except CommerceError as exc:
        raise _handle(exc) from exc

    order = result.order
    return {
        "succeeded": result.succeeded,
        "payment_status": str(result.payment_status),
        "decline_reason": str(result.decline_reason) if result.decline_reason else None,
        "order": None
        if order is None
        else {
            "order_id": order.order_id,
            "status": str(order.status),
            "grand_total": _money(order.grand_total),
            "amount_paid": _money(order.amount_paid),
        },
    }