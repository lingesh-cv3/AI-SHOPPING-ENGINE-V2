"""Kettle & Bloom GraphQL API.

One endpoint, POST /graphql, taking {"query": "...", "variables": {...}} and
returning {"data": {...}} or {"errors": [...]}. Nothing like Northfield's REST.

This is a simplified GraphQL server, not a spec-complete one. It dispatches on the
operation name in the query string rather than parsing the document, and it ignores
field selection - every query returns the full object. That is honest to state, and
it does not weaken what this exists to prove: the adapter has to build a GraphQL
request body, send it to a single endpoint, and unwrap a data/errors envelope. All
of that differs from REST, which is the difference being tested.

Errors here use proper HTTP status codes and a GraphQL errors array with an
extensions code. Northfield returns HTTP 200 with an error key. Two platforms, two
error conventions, one normalized ErrorCode by the time the engine sees it.
"""

from __future__ import annotations

import re

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import store

app = FastAPI(title="Kettle & Bloom GraphQL", version="0.1.0")


class GraphQLBody(BaseModel):
    query: str
    variables: dict = {}
    operationName: str | None = None


def _error(code: str, message: str, status: int = 400) -> JSONResponse:
    """A GraphQL error envelope, with a real HTTP status alongside it."""
    return JSONResponse(
        status_code=status,
        content={"errors": [{"message": message, "extensions": {"code": code}}]},
    )


def _ok(field: str, value) -> dict:
    return {"data": {field: value}}


#: Maps a store error string to an HTTP status. Not-found things get 404, refusals
#: get 409, everything else is a bad request.
_STATUS = {
    "PRODUCT_NOT_FOUND": 404,
    "BAG_NOT_FOUND": 404,
    "ORDER_NOT_FOUND": 404,
    "LINE_NOT_FOUND": 404,
    "OPTION_NOT_FOUND": 404,
    "DISCOUNT_NOT_FOUND": 404,
    "OUT_OF_STOCK": 409,
    "ALREADY_PAID": 409,
    "DISCOUNT_INACTIVE": 409,
    "DISCOUNT_MIN_SPEND": 409,
}


def _operation(query: str) -> str:
    """Pull the root field name out of the query.

    Crude on purpose - see the module docstring. A real server parses the document.
    """
    match = re.search(r"\{\s*([A-Za-z_][A-Za-z0-9_]*)", query)
    return match.group(1) if match else ""


@app.post("/graphql")
def graphql(body: GraphQLBody):
    op = body.operationName or _operation(body.query)
    v = body.variables or {}

    match op:
        case "collections":
            return _ok("collections", store.collections())

        case "products":
            found = store.search(v.get("search"), v.get("collection"), v.get("limit", 20))
            return _ok("products", found)

        case "product":
            item = store.product(v.get("id", ""))
            if item is None:
                return _error("PRODUCT_NOT_FOUND", "no product with that id", 404)
            return _ok("product", item)

        case "bag":
            found = store.bag(v.get("id", ""))
            if found is None:
                return _error("BAG_NOT_FOUND", "no bag with that id", 404)
            return _ok("bag", found)

        case "order":
            found = store.order(v.get("id", ""))
            if found is None:
                return _error("ORDER_NOT_FOUND", "no order with that id", 404)
            return _ok("order", found)

        case "createBag":
            return _ok("createBag", store.create_bag())

        case "addLine":
            r = store.add_line(
                v.get("bagId", ""),
                v.get("productId", ""),
                v.get("optionId"),
                v.get("quantity", 1),
            )
            if isinstance(r, str):
                return _error(r, r.replace("_", " ").lower(), _STATUS.get(r, 400))
            return _ok("addLine", r)

        case "updateLine":
            r = store.update_line(
                v.get("bagId", ""), v.get("lineId", ""), v.get("quantity", 1)
            )
            if isinstance(r, str):
                return _error(r, r.replace("_", " ").lower(), _STATUS.get(r, 400))
            return _ok("updateLine", r)

        case "applyDiscount":
            r = store.apply_discount(v.get("bagId", ""), v.get("code", ""))
            if isinstance(r, str):
                return _error(r, r.replace("_", " ").lower(), _STATUS.get(r, 400))
            return _ok("applyDiscount", r)

        case "placeOrder":
            r = store.place_order(
                v.get("bagId", ""),
                v.get("cardLast4", "1111"),
                v.get("idempotencyKey", ""),
            )
            if isinstance(r, str):
                return _error(r, r.replace("_", " ").lower(), _STATUS.get(r, 400))
            return _ok("placeOrder", r)

        case "retryPayment":
            # The operation Northfield does not have at all.
            r = store.retry_payment(
                v.get("orderId", ""),
                v.get("method", ""),
                v.get("idempotencyKey", ""),
            )
            if isinstance(r, str):
                return _error(r, r.replace("_", " ").lower(), _STATUS.get(r, 400))
            return _ok("retryPayment", r)

        case "resetStore":
            store.reset()
            return _ok("resetStore", True)

    return _error("UNKNOWN_OPERATION", f"no operation named {op!r}", 400)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "kettle-and-bloom", "protocol": "graphql"}
