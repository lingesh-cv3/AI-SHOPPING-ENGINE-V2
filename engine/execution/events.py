"""Minimal shopper-journey interaction instrumentation - the Phase 2
foundation for later personalized ranking, nothing more.

Deliberately not wired into ranking, synthesis, or memory this phase. A
future pass can read `ShopperEvent` rows to build real interaction-based
personalization; this one only establishes where those facts get written,
so that future pass has real history to read rather than starting from
zero.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from engine.db.models import ShopperEvent
from engine.db.session import session_scope


class EventType:
    """The fixed, small set of events this pass records. A string constant
    class rather than an enum for the same append-only reasoning
    `ShopperEvent.event_type`'s own docstring gives."""

    PRODUCT_IMPRESSION = "product_impression"
    COMPARISON_STARTED = "comparison_started"
    ADD_TO_CART = "add_to_cart"
    REMOVE_FROM_CART = "remove_from_cart"
    CHECKOUT_STARTED = "checkout_started"
    PURCHASE_COMPLETED = "purchase_completed"


async def record(
    *,
    connection_id: str,
    session_id: str,
    event_type: str,
    shopper_id: str | None = None,
    product_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Write one event. Best-effort is the caller's job (see chat.py) - this
    function does not itself swallow errors, so a genuine schema/connection
    problem is still visible to whatever wraps it."""
    async with session_scope() as db:
        db.add(
            ShopperEvent(
                event_id=f"evt_{uuid.uuid4().hex[:16]}",
                connection_id=connection_id,
                session_id=session_id,
                shopper_id=shopper_id,
                event_type=event_type,
                product_id=product_id,
                detail=detail or {},
            )
        )


async def shortlist_engagement(
    connection_id: str, *, days: int = 30
) -> dict[str, int | float | None]:
    """The one real, directional figure this instrumentation makes possible:
    of the products actually shown on a mission-aware shortlist
    (`product_impression`), how many were then added to cart in the same
    session.

    Deliberately not wired into any Merchant/Operations surface this phase -
    that would be a Merchant-facing change this phase's own operating rules
    scope out. This exists so the question "did the shortlist mechanism do
    anything?" has a real, queryable answer instead of a write-only log
    nobody ever reads - see PROGRESS.md for how this was actually run and
    read, and by whom.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as db:
        impressions = (
            (
                await db.execute(
                    select(ShopperEvent.session_id, ShopperEvent.product_id).where(
                        ShopperEvent.connection_id == connection_id,
                        ShopperEvent.event_type == EventType.PRODUCT_IMPRESSION,
                        ShopperEvent.created_at >= since,
                    )
                )
            )
            .all()
        )
        if not impressions:
            return {"impressions": 0, "converted": 0, "rate": None}

        adds = (
            (
                await db.execute(
                    select(ShopperEvent.session_id, ShopperEvent.product_id).where(
                        ShopperEvent.connection_id == connection_id,
                        ShopperEvent.event_type == EventType.ADD_TO_CART,
                        ShopperEvent.created_at >= since,
                    )
                )
            )
            .all()
        )
        added_pairs = {(row.session_id, row.product_id) for row in adds}

        seen: set[tuple[str, str]] = set()
        converted = 0
        for row in impressions:
            key = (row.session_id, row.product_id)
            if key in seen:
                continue
            seen.add(key)
            if key in added_pairs:
                converted += 1

    total = len(seen)
    return {
        "impressions": total,
        "converted": converted,
        "rate": (converted / total) if total else None,
    }


async def record_many(
    *,
    connection_id: str,
    session_id: str,
    event_type: str,
    shopper_id: str | None,
    product_ids: list[str],
) -> None:
    """One row per product - `product_impression` on a shortlist of N shown
    products is N events, not one event carrying a list, so a later
    per-product aggregate ("how often was this product shown") is a plain
    group-by rather than needing to unpack JSON first."""
    if not product_ids:
        return
    async with session_scope() as db:
        for product_id in product_ids:
            db.add(
                ShopperEvent(
                    event_id=f"evt_{uuid.uuid4().hex[:16]}",
                    connection_id=connection_id,
                    session_id=session_id,
                    shopper_id=shopper_id,
                    event_type=event_type,
                    product_id=product_id,
                    detail={},
                )
            )
