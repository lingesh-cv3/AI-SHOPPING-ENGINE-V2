"""Sample merchant HTTP API.

Runs as its own process on port 8001. The adapter reaches it over HTTP exactly
as it would a real merchant backend - no shared code, no imports from shared/.

Deliberately awkward, and every bit of it is something real platforms do:
- "items" not products, "basket" not cart, "purchase" not order, "voucher" not promotion
- money as integer paise, not Decimal with a currency
- errors as HTTP 200 with an {"error": "CODE"} body
- stock as "Y" / "N" / "LOW", not a boolean or a count
- NO refund endpoint and NO payment-recovery endpoint

That last one matters most: it forces the adapter to declare those capabilities
unsupported, which exercises the path every real merchant will eventually hit.
"""

from __future__ import annotations

from fastapi import FastAPI
from pydantic import BaseModel

from .. import store

app = FastAPI(title="Sample Merchant Backend", version="0.1.0")

API = "/api/v1"


def _err(code: str) -> dict:
    """Errors return HTTP 200 with an error key. Bad practice, faithfully copied.

    An adapter that only checks status codes will treat these as success. That
    is the bug worth catching now rather than in production.
    """
    return {"error": code}


class AddLineBody(BaseModel):
    product_id: str
    variant_ref: str | None = None
    qty: int = 1


class UpdateLineBody(BaseModel):
    qty: int


class VoucherBody(BaseModel):
    code: str


class PurchaseBody(BaseModel):
    basket_ref: str
    card_last4: str = "1111"
    idem_key: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "sample-merchant"}


@app.get(f"{API}/departments")
def list_departments() -> dict:
    """Every department in the catalog, for storefront navigation."""
    return {"departments": store.list_departments()}


@app.get(f"{API}/items")
def list_items(q: str = "", limit: int = 20, dept: str | None = None) -> dict:
    """Browse or search. An empty list is a valid result, never an error.

    With no q this browses, optionally filtered to one department. With a q it is
    a literal substring match on the title, which is what produces the dead
    searches the engine recovers from.
    """
    hits, total = store.search_items(q, limit, dept)
    return {"items": hits, "match_count": total}

@app.get(f"{API}/items/{{product_id}}")
def get_item(product_id: str) -> dict:
    item = store.get_item(product_id)
    return {"item": item} if item else _err("ITEM_NOT_FOUND")


@app.get(f"{API}/stock/{{product_id}}")
def get_stock(product_id: str, variant_ref: str | None = None) -> dict:
    stock = store.get_stock(product_id, variant_ref)
    if stock is None:
        return _err("ITEM_NOT_FOUND" if variant_ref is None else "VARIANT_NOT_FOUND")
    return {"stock": stock}


@app.post(f"{API}/basket")
def create_basket() -> dict:
    return {"basket": store.create_basket()}


@app.get(f"{API}/basket/{{basket_ref}}")
def get_basket(basket_ref: str) -> dict:
    basket = store.get_basket(basket_ref)
    return {"basket": basket} if basket else _err("BASKET_MISSING")


@app.post(f"{API}/basket/{{basket_ref}}/lines")
def add_line(basket_ref: str, body: AddLineBody) -> dict:
    r = store.add_line(basket_ref, body.product_id, body.variant_ref, body.qty)
    return _err(r) if isinstance(r, str) else {"basket": r}


@app.patch(f"{API}/basket/{{basket_ref}}/lines/{{line_ref}}")
def update_line(basket_ref: str, line_ref: str, body: UpdateLineBody) -> dict:
    r = store.update_line(basket_ref, line_ref, body.qty)
    return _err(r) if isinstance(r, str) else {"basket": r}


@app.post(f"{API}/basket/{{basket_ref}}/voucher")
def apply_voucher(basket_ref: str, body: VoucherBody) -> dict:
    r = store.apply_voucher(basket_ref, body.code)
    return _err(r) if isinstance(r, str) else {"basket": r}


@app.post(f"{API}/purchase")
def create_purchase(body: PurchaseBody) -> dict:
    """Cards ending 0002, 0003, 0004 always fail with different refusal codes.

    A declined purchase still returns HTTP 200 with pay_state REFUSED. It is not
    an error - it is an unpaid order, and the whole recovery flow hangs on that
    distinction.
    """
    r = store.create_purchase(body.basket_ref, body.card_last4, body.idem_key)
    return _err(r) if isinstance(r, str) else {"purchase": r}


@app.get(f"{API}/purchase/{{purchase_ref}}")
def get_purchase(purchase_ref: str) -> dict:
    p = store.get_purchase(purchase_ref)
    return {"purchase": p} if p else _err("PURCHASE_NOT_FOUND")


@app.get(f"{API}/purchases")
def list_purchases() -> dict:
    """Convenience view for demos, not part of any adapter flow."""
    return {"purchases": store.list_purchases()}


@app.post(f"{API}/_reset")
def reset() -> dict:
    """Clear baskets and purchases. For demos and tests, not a real platform API."""
    store.reset()
    return {"ok": True}


# NOTE: no refund endpoint, no payment-recovery endpoint. This platform cannot
# do those things. The adapter must declare them unsupported and the engine must
# escalate to a human rather than improvise a workaround.