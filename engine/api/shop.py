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

from fastapi import Depends, APIRouter, HTTPException
from pydantic import BaseModel, Field

from shared.models import CapabilityUnsupported, CommerceError, Money

from engine import db

from .auth import Visitor, require_owner, shopper_scoped, visitor
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


class ChangeLine(BaseModel):
    """How many of a line the shopper wants. Zero means take it out."""

    quantity: int = Field(ge=0, le=99)


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

async def _already_bought(adapter, order_id: str | None) -> dict:
    """The answer for a basket that has already been paid for.

    The same shape a fresh checkout returns, so the storefront needs no second
    branch - it lands on the order page it would have landed on, which is where a
    shopper pressing Pay again wanted to be anyway. `already_paid` is there for
    anything that wants to say so rather than infer it.
    """
    order = None
    if order_id:
        try:
            order = await adapter.get_order(order_id)
        except CommerceError:
            # The order exists in our ledger and the platform will not hand it
            # over. Still better than charging again.
            order = None

    return {
        "succeeded": True,
        "already_paid": True,
        "payment_status": "CAPTURED",
        "decline_reason": None,
        "order": None
        if order is None
        else {
            "order_id": order.order_id,
            "status": str(order.status),
            "grand_total": _money(order.grand_total),
            "amount_paid": _money(order.amount_paid),
        },
    }


async def _mine(who: Visitor, connection_id: str, kind: str, resource_id: str) -> None:
    """Refuse unless this cart or order belongs to the caller.

    The message is the one a shopper would get for a cart that has gone. That is
    not a euphemism - from where they are standing the two are the same thing, and
    the storefront already knows how to recover from it by starting a fresh one.
    """
    codes = {
        db.owners.CART: ("CART_NOT_FOUND", "no such cart"),
        db.owners.ORDER: ("ORDER_NOT_FOUND", "no such order"),
    }
    code, message = codes[kind]
    await require_owner(
        who, connection_id, kind, resource_id, code=code, message=message
    )


@router.get("/{connection_id}/departments")
async def departments(
    connection_id: str,
    _=Depends(shopper_scoped()),
) -> dict:
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
async def get_order(
    connection_id: str, order_id: str,
    who: Visitor = Depends(visitor),
    _=Depends(shopper_scoped()),
) -> dict:
    """Look up an order.

    Works for paid and unpaid orders alike. A shopper whose card was declined
    still has an order, and being able to look it up is part of what makes the
    sale recoverable rather than lost.

    Theirs, though. Order ids run ORD00001, ORD00002, and this route used to hand
    any of them - with lines, totals and what was paid - to anybody holding the
    publishable key that ships in the page.
    """
    await _mine(who, connection_id, db.owners.ORDER, order_id)
    adapter = _adapter(connection_id)
    try:
        return _order(await adapter.get_order(order_id))
    except CommerceError as exc:
        raise _handle(exc) from exc

@router.get("/{connection_id}/search")
async def search(
    connection_id: str, q: str = "", limit: int = 24, dept: str | None = None,
    _=Depends(shopper_scoped()),
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
async def product(
    connection_id: str, product_id: str,
    _=Depends(shopper_scoped()),
) -> dict:
    adapter = _adapter(connection_id)
    try:
        return _product(await adapter.get_product(product_id))
    except CommerceError as exc:
        raise _handle(exc) from exc


@router.post("/{connection_id}/cart")
async def create_cart(
    connection_id: str,
    who: Visitor = Depends(visitor),
    _=Depends(shopper_scoped()),
) -> dict:
    """Start a basket, and record whose it is.

    Claimed here rather than on first use, because this is the one moment the
    answer is not a guess: the browser asking for a cart is the browser that will
    be filling it.
    """
    adapter = _adapter(connection_id)
    try:
        cart = await adapter.create_cart()
    except CommerceError as exc:
        raise _handle(exc) from exc

    # Taken rather than claimed. The shop has just minted this id, so whoever is
    # holding it owns it - including when a restart has reissued a number somebody
    # else held last week.
    keys = await who.keys_for(connection_id)
    await db.owners.take(connection_id, db.owners.CART, cart.cart_id, keys[0])
    return _cart(cart)


@router.get("/{connection_id}/cart/{cart_id}")
async def get_cart(
    connection_id: str, cart_id: str,
    who: Visitor = Depends(visitor),
    _=Depends(shopper_scoped()),
) -> dict:
    await _mine(who, connection_id, db.owners.CART, cart_id)
    adapter = _adapter(connection_id)
    try:
        return _cart(await adapter.get_cart(cart_id))
    except CommerceError as exc:
        raise _handle(exc) from exc


@router.post("/{connection_id}/cart/{cart_id}/lines")
async def add_line(
    connection_id: str, cart_id: str, body: AddLine,
    who: Visitor = Depends(visitor),
    _=Depends(shopper_scoped()),
) -> dict:
    await _mine(who, connection_id, db.owners.CART, cart_id)
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


@router.patch("/{connection_id}/cart/{cart_id}/lines/{line_id}")
async def change_line(
    connection_id: str,
    cart_id: str,
    line_id: str,
    body: ChangeLine,
    who: Visitor = Depends(visitor),
    _=Depends(shopper_scoped()),
) -> dict:
    """Change how many of something is in the cart. Zero removes it.

    There was no way to do this without the model, so a shopper could add things
    and not remove them - and when the provider was busy, a cart was a one-way
    door.

    Zero rather than a separate delete, because that is what the commerce
    interface offers and inventing a second verb for the same operation would mean
    two code paths where one will do.
    """
    await _mine(who, connection_id, db.owners.CART, cart_id)
    adapter = _adapter(connection_id)
    try:
        cart = await adapter.update_cart(cart_id, line_id, quantity=body.quantity)
    except CommerceError as exc:
        raise _handle(exc) from exc
    return _cart(cart)


@router.post("/{connection_id}/cart/{cart_id}/promotion")
async def apply_promotion(
    connection_id: str, cart_id: str, body: PromoBody,
    who: Visitor = Depends(visitor),
    _=Depends(shopper_scoped()),
) -> dict:
    """Apply a coupon.

    WELCOME10 works. SUMMER25 is expired, which is what drives the
    PROMOTION_FAILED friction path.
    """
    await _mine(who, connection_id, db.owners.CART, cart_id)
    adapter = _adapter(connection_id)
    try:
        await adapter.apply_promotion(cart_id, body.code)
        return _cart(await adapter.get_cart(cart_id))
    except CommerceError as exc:
        raise _handle(exc) from exc


@router.post("/{connection_id}/cart/{cart_id}/checkout")
async def checkout(
    connection_id: str, cart_id: str, body: CheckoutBody,
    who: Visitor = Depends(visitor),
    _=Depends(shopper_scoped()),
) -> dict:
    """Complete the order.

    A decline returns HTTP 200 with succeeded false and a real order. It is not an
    error - it is an unpaid order, which is precisely the thing worth recovering.
    """
    await _mine(who, connection_id, db.owners.CART, cart_id)
    adapter = _adapter(connection_id)

    # The same ledger the chat pay route uses, because this is the same money by
    # another door. This route generated a fresh uuid per request, so it had no
    # idempotency at all - not even the half the other one had. Same cart, same
    # card, three taps, three orders, which is precisely what a shopper on a slow
    # connection does when nothing happens after the first one.
    settled = await db.idempotency.begin_payment(connection_id, cart_id)

    if settled is not None:
        if settled.get("in_flight"):
            # Not an error and not a second charge. The first tap is still in the
            # air, and the honest answer is to say so rather than to start another.
            raise HTTPException(
                409,
                detail={
                    "code": "CHECKOUT_IN_PROGRESS",
                    "message": "that payment is already going through",
                    "retryable": True,
                },
            )
        return await _already_bought(adapter, settled.get("order_id"))

    # Derived from the cart and the card, and identical to the key the chat route
    # builds - so a shopper who taps Pay here and then pays in the chat with the
    # same card is one purchase to the platform as well as to us.
    key = f"pay-{cart_id}-{body.card_last4}"

    try:
        result = await adapter.checkout_with_card(
            cart_id, card_last4=body.card_last4, idempotency_key=key
        )
    except CommerceError as exc:
        # Nothing was attempted, so the basket goes back rather than staying
        # locked over a failure that was not the shopper's.
        await db.idempotency.release_payment(connection_id, cart_id)
        raise _handle(exc) from exc

    order = result.order

    # A success locks the basket; a decline leaves it buyable, because a declined
    # order is the one worth recovering and the shopper is about to be offered
    # another way to pay.
    await db.idempotency.payment_settled(
        connection_id,
        cart_id,
        succeeded=result.succeeded,
        order_id=order.order_id if order else None,
        summary=(
            f"paid {order.order_id}"
            if result.succeeded and order
            else "declined"
        ),
    )


    # The order belongs to whoever the cart belonged to. Recorded here rather than
    # left to first use, because the alternative is that the first person to count
    # up to this id owns it - and a declined order is precisely the one a shopper
    # will come back to look at later.
    if order is not None:
        keys = await who.keys_for(connection_id)
        await db.owners.take(
            connection_id, db.owners.ORDER, order.order_id, keys[0]
        )

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