"""A conversation that belongs to a shopper rather than a browser tab.

Signed in, a shopper's chat survives closing the tab and reopening tomorrow. A guest
gets what they always got: a session that lasts the visit, which is right - there is
no identity to remember a guest by, and inventing one would mean tracking somebody
who did not ask to be tracked.

Two caps, and they are deliberately different numbers.

STORED is how much history is kept and shown to the shopper: 30 turns, which is what
Raman asked for. Enough to scroll back through a previous visit.

The model still sees only the recent part, because every turn in its context is
tokens against a per-minute budget that is already the main constraint here.
Remembering more and reading less are not in conflict - the shopper wants to see
their history, and the model only needs the thread it is answering.
"""

from __future__ import annotations

from sqlalchemy import select, update

from engine.db.models import SessionTurn
from engine.db.session import session_scope

#: Turns kept per shopper, and shown to them.
#:
#: Older ones are deleted rather than archived. A shopper asking about a purchase
#: from two months ago wants the order, which order lookup answers - not the
#: conversation where they were choosing between two coffees.
STORED = 30


async def session_for(shopper_id: str, connection_id: str) -> str:
    """The conversation id for a signed-in shopper at one merchant.

    Derived rather than stored, so there is one source of truth and nothing to keep
    in step. Three separate bugs this week came from two places holding the same
    value; this avoids a fourth by not having a second place.

    Scoped to the merchant as well as the shopper, because the accounts are already
    separate - a Kettle conversation appearing at Northfield would leak one client's
    customer to another.
    """
    return f"shp_{connection_id}_{shopper_id}"


async def adopt(guest_session: str, shopper_session: str, connection_id: str) -> int:
    """Move a guest's turns onto their account. Returns how many moved.

    Called when somebody signs in mid-conversation. Signing in is meant to give a
    shopper their context, so losing the conversation they are in the middle of - at
    the exact moment they identify themselves - would be backwards.

    The risk is a shared computer, where the previous person's turns get attached to
    whoever signs in next. That is why the sign-out control has to be visible and
    why there is a "not you?" link: the mitigation is making it obvious and easy to
    undo, not refusing the common case for the rare one.
    """
    if guest_session == shopper_session:
        return 0

    async with session_scope() as db:
        result = await db.execute(
            update(SessionTurn)
            .where(
                SessionTurn.session_id == guest_session,
                SessionTurn.connection_id == connection_id,
            )
            .values(session_id=shopper_session)
        )
        moved = result.rowcount or 0

    if moved:
        await trim(shopper_session, connection_id)

    return moved


async def trim(session_id: str, connection_id: str) -> int:
    """Keep only the most recent STORED turns. Returns how many were dropped.

    Called after adopting and after each turn is written, so the cap holds without a
    sweep that could fall behind.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(SessionTurn.turn_id)
            .where(
                SessionTurn.session_id == session_id,
                SessionTurn.connection_id == connection_id,
            )
            .order_by(SessionTurn.created_at.desc())
            .offset(STORED)
        )
        stale = [row[0] for row in result]

        if not stale:
            return 0

        for turn_id in stale:
            turn = await db.get(SessionTurn, turn_id)
            if turn is not None:
                await db.delete(turn)

        return len(stale)