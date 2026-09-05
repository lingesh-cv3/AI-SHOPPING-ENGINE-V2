"""Remembering which cart belongs to which shopper.

Small on purpose. The cart itself lives on the platform - Shopify's or Kettle's -
and all we hold is which one to hand back to a returning shopper. Storing anything
more would mean keeping a copy of somebody else's data in step with theirs, which is
a class of bug worth not having.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from .models import ShopperCart
from .session import session_scope


async def remembered(shopper_id: str, connection_id: str) -> str | None:
    """The cart this shopper left here, if any."""
    async with session_scope() as db:
        result = await db.execute(
            select(ShopperCart).where(
                ShopperCart.shopper_id == shopper_id,
                ShopperCart.connection_id == connection_id,
            )
        )
        row = result.scalar_one_or_none()
        return row.cart_id if row else None


async def remember(shopper_id: str, connection_id: str, cart_id: str) -> None:
    """Note that this cart is theirs.

    Replaces rather than accumulates. A shopper has one basket at a shop, and
    keeping every cart they ever had would mean deciding which is current - a
    question with no good answer and one we would get wrong.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(ShopperCart).where(
                ShopperCart.shopper_id == shopper_id,
                ShopperCart.connection_id == connection_id,
            )
        )
        row = result.scalar_one_or_none()

        if row is None:
            db.add(
                ShopperCart(
                    row_id=f"scart_{uuid.uuid4().hex[:16]}",
                    shopper_id=shopper_id,
                    connection_id=connection_id,
                    cart_id=cart_id,
                )
            )
        else:
            row.cart_id = cart_id
            row.updated_at = datetime.now(UTC)


async def forget(shopper_id: str, connection_id: str) -> None:
    """Drop the remembered cart.

    For when it has been paid for. A finished basket handed back to a returning
    shopper is worse than an empty one - they would see things they had already
    bought and reasonably think they were about to buy them again.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(ShopperCart).where(
                ShopperCart.shopper_id == shopper_id,
                ShopperCart.connection_id == connection_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            await db.delete(row)