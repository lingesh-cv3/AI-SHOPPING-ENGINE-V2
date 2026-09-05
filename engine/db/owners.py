"""Who a cart, a conversation or an order belongs to.

Small on purpose, and the smallness is the point: this answers one question -
"has this caller met this thing before" - and nothing else. The thing itself
lives on the platform; all we hold is who reached it first.

Everything here takes a *set* of owner keys rather than one, because a caller can
legitimately be two people at once: the browser they are sitting at, and the
account they signed into from it. A shopper who fills a basket as a guest and then
signs in must not lose it, and the same shopper on their phone tomorrow must still
find it - so the browser's claim and the account's claim both have to count.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .models import ResourceOwner
from .session import session_scope

#: The kinds of thing that can be owned. Not an enum for the reason given on the
#: model: three strings written in four places is not worth a type.
CART = "cart"
SESSION = "session"
ORDER = "order"


async def owner_of(connection_id: str, kind: str, resource_id: str) -> str | None:
    """Who holds this, or None if nobody has claimed it yet."""
    async with session_scope() as db:
        result = await db.execute(
            select(ResourceOwner.owner_key).where(
                ResourceOwner.connection_id == connection_id,
                ResourceOwner.kind == kind,
                ResourceOwner.resource_id == resource_id,
            )
        )
        return result.scalar_one_or_none()


async def claim(
    connection_id: str, kind: str, resource_id: str, owner_key: str
) -> str:
    """Record this as belonging to owner_key. Returns whoever actually holds it.

    First claim wins, and a second claim is not an error - it returns the existing
    owner instead. Two tabs of the same browser creating a cart at once is ordinary
    behaviour, not something to fail a request over.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(ResourceOwner).where(
                ResourceOwner.connection_id == connection_id,
                ResourceOwner.kind == kind,
                ResourceOwner.resource_id == resource_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is not None:
            return row.owner_key

        db.add(
            ResourceOwner(
                row_id=f"own_{uuid.uuid4().hex[:16]}",
                connection_id=connection_id,
                kind=kind,
                resource_id=resource_id,
                owner_key=owner_key,
            )
        )

    return owner_key


async def claim_safely(
    connection_id: str, kind: str, resource_id: str, owner_key: str
) -> str:
    """claim(), tolerating the race the unique constraint exists to catch.

    Two requests from the same browser can reach here at the same moment - the
    storefront's mount effect and its sign-in effect both touch the cart. One of
    them loses the insert. Losing it is fine; treating it as an error would fail a
    request that did nothing wrong.
    """
    try:
        return await claim(connection_id, kind, resource_id, owner_key)
    except IntegrityError:
        existing = await owner_of(connection_id, kind, resource_id)
        return existing or owner_key


async def take(
    connection_id: str, kind: str, resource_id: str, owner_key: str
) -> None:
    """Own this outright, replacing whoever held it before.

    For the one moment the answer cannot be a guess: the platform has just minted
    this id and handed it back, so the browser holding it is the browser it belongs
    to, and any older claim on the same string is stale.

    Which is not hypothetical. Cart ids are sequential and the platform's counter
    lives in its own memory, so a merchant restart reissues BSK00001 - and with
    first-claim-wins, the shop handed a shopper a brand new basket that the engine
    then refused them, because somebody last week had owned that number. Every
    storefront on the shop broke at once and the audit stayed green, because it
    happened to be drawing ids nobody had claimed yet.

    claim() is still right everywhere else. The difference is whether we watched
    the id being created.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(ResourceOwner).where(
                ResourceOwner.connection_id == connection_id,
                ResourceOwner.kind == kind,
                ResourceOwner.resource_id == resource_id,
            )
        )
        row = result.scalar_one_or_none()

        if row is None:
            db.add(
                ResourceOwner(
                    row_id=f"own_{uuid.uuid4().hex[:16]}",
                    connection_id=connection_id,
                    kind=kind,
                    resource_id=resource_id,
                    owner_key=owner_key,
                )
            )
            return

        row.owner_key = owner_key


async def may_touch(
    connection_id: str,
    kind: str,
    resource_id: str,
    owner_keys: Iterable[str],
) -> bool:
    """Whether this caller may reach this thing.

    Unclaimed means claimed, by them, now. That is trust-on-first-use, and it is
    the compromise that lets a database full of carts nobody recorded an owner for
    keep working instead of locking every existing shopper out of their basket.
    Everything created from here on is claimed at creation, where the answer is not
    a guess.
    """
    keys = [k for k in owner_keys if k]
    if not keys:
        return False

    held = await owner_of(connection_id, kind, resource_id)

    if held is None:
        await claim_safely(connection_id, kind, resource_id, keys[0])
        return True

    return held in keys


async def adopt(
    connection_id: str,
    kind: str,
    resource_id: str,
    new_owner: str,
    from_keys: Iterable[str],
) -> bool:
    """Move something to a signed-in shopper. Returns whether it moved.

    Only from an owner the caller already holds. Without that check, signing in
    would be a way to take anything whose id you could name, which is the same hole
    one door along.
    """
    keys = [k for k in from_keys if k]

    async with session_scope() as db:
        result = await db.execute(
            select(ResourceOwner).where(
                ResourceOwner.connection_id == connection_id,
                ResourceOwner.kind == kind,
                ResourceOwner.resource_id == resource_id,
            )
        )
        row = result.scalar_one_or_none()

        if row is None:
            db.add(
                ResourceOwner(
                    row_id=f"own_{uuid.uuid4().hex[:16]}",
                    connection_id=connection_id,
                    kind=kind,
                    resource_id=resource_id,
                    owner_key=new_owner,
                )
            )
            return True

        if row.owner_key == new_owner:
            return False

        if row.owner_key not in keys:
            return False

        row.owner_key = new_owner
        return True
