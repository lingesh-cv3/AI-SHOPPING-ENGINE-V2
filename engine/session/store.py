"""Session memory, shared across both surfaces.

The blueprint committed to one memory serving both the friction widget and the
open-ended chat. This module is that commitment.

What makes it shared is not clever machinery - it is that both surfaces write
against the same session_id. A declined payment creates a Case carrying that id; a
chat message creates a SessionTurn carrying the same one. When the next turn
assembles its context, it reads both. The shopper does not have to explain that
their card was declined, because the engine already recorded it against them.

Two deliberate limits:

History is trimmed, not summarised. The last few turns go in verbatim; older ones
are dropped. Summarising would need another model call per turn, and on a
per-minute token budget that cost buys less than it spends.

Friction is read as fact, not as conversation. A case enters the context as a
recorded event ("their payment was declined"), not as something the assistant said.
The assistant should know it happened without claiming to have discussed it.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select

from ..db.models import Case, SessionTurn
from ..db.session import session_scope

#: How many turns of conversation to carry. Six is roughly three exchanges, which
#: covers "no, cheaper than that" style follow-ups without spending the token
#: budget on a whole visit's history.
HISTORY_TURNS = 6

#: How many recent friction events to surface. Two is enough for "my payment failed
#: and then my coupon didn't work"; more starts to read as a complaint log.
RECENT_CASES = 2


def new_session_id() -> str:
    """A session id the storefront holds for the length of a visit."""
    return f"sess_{uuid.uuid4().hex[:16]}"


async def add_turn(
    *,
    session_id: str,
    connection_id: str,
    speaker: str,
    text: str,
    case_id: str | None = None,
) -> None:
    """Record one thing that was said."""
    async with session_scope() as db:
        db.add(
            SessionTurn(
                turn_id=f"turn_{uuid.uuid4().hex[:16]}",
                session_id=session_id,
                connection_id=connection_id,
                speaker=speaker,
                text=text,
                case_id=case_id,
            )
        )


async def history(
    session_id: str, connection_id: str, *, limit: int = HISTORY_TURNS
) -> list[str]:
    """The recent conversation, oldest first, formatted for the model.

    Queried newest-first with a limit and then reversed, rather than fetched whole
    and sliced. On a long visit the difference is the entire conversation crossing
    the wire versus six rows.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(SessionTurn)
            .where(
                SessionTurn.session_id == session_id,
                SessionTurn.connection_id == connection_id,
            )
            .order_by(SessionTurn.created_at.desc())
            .limit(limit)
        )
        turns = list(result.scalars())

    turns.reverse()
    return [
        f"{'Shopper' if t.speaker == 'shopper' else 'You'}: {t.text}" for t in turns
    ]


async def recent_friction(
    session_id: str, connection_id: str, *, limit: int = RECENT_CASES
) -> list[str]:
    """Friction this shopper hit during this visit, as recorded fact.

    This is the shared part. These cases were created by the widget when the
    storefront detected a problem - not by anything the shopper typed. Reading them
    here is what lets the chat know about a declined payment nobody mentioned.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(Case)
            .where(
                Case.session_id == session_id,
                Case.connection_id == connection_id,
                Case.friction_type.is_not(None),
            )
            .order_by(Case.created_at.desc())
            .limit(limit)
        )
        cases = list(result.scalars())

    lines = []
    for case in cases:
        parts = [f"they hit {case.friction_type}"]
        if case.query:
            parts.append(f'while searching "{case.query}"')
        if case.order_id:
            parts.append(f"on order {case.order_id}")
        if case.diagnosis:
            parts.append(f"- {case.diagnosis}")
        lines.append(" ".join(parts))
    return lines


async def context_for(
    session_id: str, connection_id: str
) -> tuple[list[str], list[str]]:
    """Both halves of the shared memory, for one turn.

    Returned as a pair rather than merged, because the two are different kinds of
    thing and the context builder labels them differently. Merging them would let
    the assistant treat a recorded event as something it had said.
    """
    return (
        await history(session_id, connection_id),
        await recent_friction(session_id, connection_id),
    )