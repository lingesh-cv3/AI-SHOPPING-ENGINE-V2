"""Structured cart-change proposals - the "model suggestion -> stored
proposal -> shopper approval -> execution" layer.

See `CartProposal`'s own docstring in `models.py` for the full shape and why
it exists. This module is pure persistence: create a pending row, list what
is pending for a cart, and move a row's state forward. It never itself
decides whether a proposal is still valid or executes anything - that is
`engine/api/chat.py::accept_proposal`'s job, so the revalidate-then-execute
logic lives in exactly one place rather than being duplicated per caller.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import select

from .models import CartProposal
from .session import session_scope

#: A pending proposal older than this is treated as stale even before any
#: catalog/cart check runs - the cart or the shopper's intent has very
#: likely moved on. Generous enough not to expire one a shopper is still
#: actively deciding on mid-conversation.
STALE_AFTER_MINUTES = 30


async def create(
    *,
    connection_id: str,
    session_id: str,
    shopper_id: str | None,
    cart_id: str,
    change_type: str,
    product_id: str,
    variant_id: str | None = None,
    quantity: int | None = None,
    replaces_product_id: str | None = None,
    reason: str,
    evidence: list[str],
    snapshot_price: str | None = None,
) -> dict:
    proposal_id = f"prop_{uuid.uuid4().hex[:16]}"
    async with session_scope() as db:
        row = CartProposal(
            proposal_id=proposal_id,
            connection_id=connection_id,
            session_id=session_id,
            shopper_id=shopper_id,
            cart_id=cart_id,
            change_type=change_type,
            product_id=product_id,
            variant_id=variant_id,
            quantity=quantity,
            replaces_product_id=replaces_product_id,
            reason=reason,
            evidence=evidence,
            snapshot_price=snapshot_price,
            state="PENDING",
        )
        db.add(row)
    return _as_dict(row)


async def get(connection_id: str, proposal_id: str) -> dict | None:
    async with session_scope() as db:
        result = await db.execute(
            select(CartProposal).where(
                CartProposal.connection_id == connection_id,
                CartProposal.proposal_id == proposal_id,
            )
        )
        row = result.scalar_one_or_none()
    return _as_dict(row) if row else None


async def pending_for_cart(
    connection_id: str, cart_id: str, session_id: str | None = None
) -> list[dict]:
    """What a shopper still has to decide on for this cart, oldest first -
    so the earliest suggestion is the one they see first, the same reading
    order the conversation itself would have offered it in.

    `session_id` is optional only for backward compatibility with any other
    caller that genuinely wants every proposal for a cart regardless of
    which conversation created it (there is none today - grep confirms
    `engine/api/chat.py::list_proposals` is the sole caller, and it always
    has a real session_id to pass). Found live: a shopper who owns more
    than one session over time (a real, ordinary case - re-opening the
    shop later, or a demo/test identity reused across runs) can have more
    than one cart of their own on record; `_mine`'s ownership check
    confirms `session_id` and `cart_id` each independently belong to the
    caller, but never that this SPECIFIC cart_id is the one THIS SPECIFIC
    session created - so without this filter, a request for one of your
    own conversations' pending proposals could surface a stale proposal
    from a different conversation of your own that happens to reference a
    cart_id you also separately own. Not a cross-shopper exposure (a
    stranger's own session_id/cart_id pair still fails `_mine` before this
    function ever runs) - a same-shopper, cross-conversation data-mixing
    bug, closed the same way session/cart ownership is already scoped
    everywhere else in this codebase."""
    async with session_scope() as db:
        conditions = [
            CartProposal.connection_id == connection_id,
            CartProposal.cart_id == cart_id,
            CartProposal.state == "PENDING",
        ]
        if session_id is not None:
            conditions.append(CartProposal.session_id == session_id)
        result = await db.execute(
            select(CartProposal)
            .where(*conditions)
            .order_by(CartProposal.created_at.asc())
        )
        rows = list(result.scalars())
    return [_as_dict(r) for r in rows]


async def needs_choice_for_cart_product(
    connection_id: str, cart_id: str, product_id: str
) -> dict | None:
    """The most recent NEEDS_CHOICE proposal for this exact cart+product, if
    any - used only to link an eventual ordinary tap-add (resolving the
    size/variant a NEEDS_CHOICE proposal itself could not capture) back to
    the multi-item queue it came from, so `events.multi_item_completion_rate`
    can count it as completed. Read-only lookup; never mutates state itself
    - the caller decides what, if anything, to do with the match."""
    async with session_scope() as db:
        result = await db.execute(
            select(CartProposal)
            .where(
                CartProposal.connection_id == connection_id,
                CartProposal.cart_id == cart_id,
                CartProposal.product_id == product_id,
                CartProposal.state == "NEEDS_CHOICE",
            )
            .order_by(CartProposal.created_at.desc())
        )
        row = result.scalars().first()
    return _as_dict(row) if row else None


async def set_state(
    connection_id: str,
    proposal_id: str,
    *,
    state: str,
    execution_result: dict | None = None,
) -> dict | None:
    """Move a proposal's state forward. Never validates the transition
    itself - the caller (engine/api/proposals.py) already decided this is
    the right next state after its own checks; this just persists it."""
    async with session_scope() as db:
        result = await db.execute(
            select(CartProposal).where(
                CartProposal.connection_id == connection_id,
                CartProposal.proposal_id == proposal_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        row.state = state
        row.decided_at = datetime.now(UTC)
        if execution_result is not None:
            row.execution_result = execution_result
        result_dict = _as_dict(row)
    return result_dict


def is_stale(proposal: dict) -> bool:
    """Whether a PENDING proposal is old enough that it must be
    revalidated against live data rather than trusted as-is - see
    `engine/api/proposals.py::accept` for the actual revalidation
    (fresh product fetch, price/availability compared to the snapshot)."""
    created = proposal.get("created_at")
    if not created:
        return False
    if isinstance(created, str):
        created = datetime.fromisoformat(created)
    if created.tzinfo is None:
        # SQLite round-trips a timezone-aware datetime as naive - this
        # column is always written as UTC (`_now()` in models.py), so a
        # naive value read back is UTC, not local time.
        created = created.replace(tzinfo=UTC)
    age = datetime.now(UTC) - created
    return age.total_seconds() > STALE_AFTER_MINUTES * 60


def _as_dict(row: CartProposal | None) -> dict | None:
    if row is None:
        return None
    return {
        "proposal_id": row.proposal_id,
        "connection_id": row.connection_id,
        "session_id": row.session_id,
        "shopper_id": row.shopper_id,
        "cart_id": row.cart_id,
        "change_type": row.change_type,
        "product_id": row.product_id,
        "variant_id": row.variant_id,
        "quantity": row.quantity,
        "replaces_product_id": row.replaces_product_id,
        "reason": row.reason,
        "evidence": row.evidence or [],
        "snapshot_price": row.snapshot_price,
        "state": row.state,
        "execution_result": row.execution_result,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "decided_at": row.decided_at.isoformat() if row.decided_at else None,
    }
