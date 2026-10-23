"""The detail of one shopper follow-up, as the merchant who owns it sees it.

Presentation only: it reads, it decides nothing, and it calls no platform adapter,
so it works when the merchant's platform is down.

Everything comes from data the engine already holds and the merchant is entitled
to, for a case that is theirs:

- the case itself (what the shopper hit, what the engine concluded, what the
  shopper was told, what it could not do);
- the conversation, as plain text - speaker, words, time - and nothing else from
  the turn record (no product cards, no payment widgets);
- who the shopper is, but only when that is actually known: the conversation's owner
  must be a signed-in shopper account of THIS merchant. A guest, an unowned session
  (a webhook) or an account belonging to a different merchant is reported as
  "no account", never guessed and never looked up across merchants.
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine import db
from engine import session as session_store

#: How much of the conversation the detail carries. Enough to see what the shopper
#: said and was told around the hand-over; not the whole history of a visit.
CONVERSATION_TURNS = 12


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


async def _shopper_for(connection_id: str, session_id: str | None) -> dict:
    """Who the shopper is, or why that is not known."""
    if not session_id:
        return {"account": None, "why": "no conversation is recorded for this case"}

    owner = await db.owners.owner_of(connection_id, db.owners.SESSION, session_id)
    if owner is None:
        return {"account": None, "why": "no shopper is recorded for this conversation"}
    if not owner.startswith("shp_"):
        return {
            "account": None,
            "why": "the shopper was not signed in, so there is no contact on record",
        }

    shopper = await db.shoppers.by_id(owner)
    if shopper is None or shopper["connection_id"] != connection_id:
        # An account that is not this merchant's is never shown to this merchant.
        return {"account": None, "why": "no account of yours is linked to this conversation"}

    return {
        "account": {
            "display_name": shopper["display_name"],
            "username": shopper["username"],
            "email": shopper["email"],
        },
        "why": None,
    }


async def detail(connection_id: str, case) -> dict:
    """The full context of one follow-up. `case` must already be scoped to the merchant."""
    now = datetime.now(UTC)
    created = _aware(case.created_at)
    handled_at = _aware(case.handled_at)

    turns: list[dict] = []
    if case.session_id:
        raw = await session_store.turns(
            case.session_id, connection_id, limit=CONVERSATION_TURNS
        )
        turns = [
            {"speaker": t["speaker"], "text": t["text"], "at": t["at"]} for t in raw
        ]

    return {
        "case_id": case.case_id,
        "created_at": created.isoformat() if created else None,
        "waiting_minutes": (
            int((now - created).total_seconds() / 60)
            if created and handled_at is None
            else None
        ),
        "friction_type": case.friction_type,
        "diagnosis": case.diagnosis,
        "evidence": case.evidence,
        "used_model": case.used_model,
        "shopper_reply": case.shopper_reply,
        "rejected": case.rejected,
        "order_id": case.order_id,
        "query": case.query,
        "handled": (
            {
                "at": handled_at.isoformat(),
                "by": case.handled_by,
                "note": case.handled_note,
            }
            if handled_at is not None
            else None
        ),
        "shopper": await _shopper_for(connection_id, case.session_id),
        "conversation": turns,
        "conversation_limit": CONVERSATION_TURNS,
    }
