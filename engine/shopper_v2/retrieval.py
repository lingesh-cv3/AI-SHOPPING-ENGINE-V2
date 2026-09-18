"""Retrieval for Stage 1: a thin, read-only wrapper over the EXISTING
adapter's `search_products` - no new search infrastructure, no caching, no
embeddings, no new adapter method, no adapter file touched.

Failure handling: if the adapter raises (platform down), this returns an
empty result with `unavailable=True` rather than letting the exception
reach the shopper or fabricating a product list - matching invariant 5
(raw platform errors never reach shoppers) and the "platform down" real-
client condition.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from shared.models import Product

logger = logging.getLogger(__name__)

_DEFAULT_LIMIT = 20


@dataclass
class RetrievalResult:
    products: list[Product]
    unavailable: bool = False


async def retrieve(adapter, query: str, *, limit: int = _DEFAULT_LIMIT) -> RetrievalResult:
    """Call the adapter's own `search_products(query, limit=...)` -
    identical interface V1 already uses (see `engine/api/chat.py`,
    `engine/execution/service.py`). Returns an empty, honestly-marked
    result on any adapter failure rather than raising further or
    inventing products.

    `search_products` returns a `ProductSearchResult` (query, products,
    total_available), not a bare list - see `shared/models/commerce.py`.
    This unwraps `.products` and never reads `total_available`, since
    Stage 1 makes no "this is the whole catalog" claim of any kind.
    """
    try:
        result = await adapter.search_products(query, limit=limit)
    except Exception:  # noqa: BLE001 - any adapter failure degrades the same way
        logger.warning("shopper_v2 retrieval: adapter search failed", exc_info=True)
        return RetrievalResult(products=[], unavailable=True)
    products = getattr(result, "products", result)
    return RetrievalResult(products=list(products or []), unavailable=False)
