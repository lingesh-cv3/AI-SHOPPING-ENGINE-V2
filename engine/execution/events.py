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
    #: The cart-proposal lifecycle - see engine/db/models.py::CartProposal.
    #: Each is a real, observed event (a row was created/accepted/rejected/
    #: executed), never a model claim about what it intended to do.
    PROPOSAL_CREATED = "proposal_created"
    PROPOSAL_ACCEPTED = "proposal_accepted"
    PROPOSAL_REJECTED = "proposal_rejected"
    PROPOSAL_EXECUTED = "proposal_executed"
    #: A real, evidence-grounded return-risk finding was shown to the
    #: shopper - see engine/execution/return_risk.py. Recorded so a future
    #: pass could measure whether shoppers who saw a flag chose the safer
    #: alternative more often - not measured by this pass itself.
    RETURN_RISK_FLAGGED = "return_risk_flagged"


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
    """A raw instrumentation figure over the mission-aware shortlist
    mechanism - **not evidence of business value**, and not to be cited as
    one. See PROGRESS.md's hardening-pass entry for the full account of why:
    no baseline/control exists (there is nothing to compare this rate
    against - an unranked shortlist's own rate is not measured), sample
    sizes seen in practice are far too small to support any statistical
    claim, and most of the traffic behind any number this returns during
    development is test traffic, not real shopper behaviour. Keep this as
    instrumentation groundwork for a future, properly designed evaluation
    (a real baseline, a real sample size, a real read of the numbers by
    someone who acts on them) - do not read a rate out of this function and
    call it proof of anything.

    Definition, precisely: of the products actually shown on a mission-aware
    shortlist (`product_impression`), how many were *later* added to cart in
    the same session - "later" is enforced by timestamp, not mere
    co-occurrence. A hardening-pass fix: the earlier version counted a pair
    as "converted" whenever both event types existed for it anywhere in the
    window, with no ordering check at all - an add-to-cart that happened
    *before* the product was ever shown on a shortlist (e.g. added from a
    product page, then coincidentally shown again in an unrelated shortlist
    later) would have counted as if the shortlist caused it. Only the
    earliest impression and earliest add per (session, product) pair are
    compared, and only a genuinely later add counts.

    Deliberately not wired into any Merchant/Operations surface this phase -
    that would be a Merchant-facing change this phase's own operating rules
    scope out.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as db:
        impression_rows = (
            await db.execute(
                select(
                    ShopperEvent.session_id,
                    ShopperEvent.product_id,
                    ShopperEvent.created_at,
                ).where(
                    ShopperEvent.connection_id == connection_id,
                    ShopperEvent.event_type == EventType.PRODUCT_IMPRESSION,
                    ShopperEvent.created_at >= since,
                )
            )
        ).all()
        if not impression_rows:
            return {"impressions": 0, "converted": 0, "rate": None}

        add_rows = (
            await db.execute(
                select(
                    ShopperEvent.session_id,
                    ShopperEvent.product_id,
                    ShopperEvent.created_at,
                ).where(
                    ShopperEvent.connection_id == connection_id,
                    ShopperEvent.event_type == EventType.ADD_TO_CART,
                    ShopperEvent.created_at >= since,
                )
            )
        ).all()

        earliest_impression: dict[tuple[str, str], object] = {}
        for row in impression_rows:
            key = (row.session_id, row.product_id)
            if key not in earliest_impression or row.created_at < earliest_impression[key]:
                earliest_impression[key] = row.created_at

        earliest_add: dict[tuple[str, str], object] = {}
        for row in add_rows:
            key = (row.session_id, row.product_id)
            if key not in earliest_add or row.created_at < earliest_add[key]:
                earliest_add[key] = row.created_at

        converted = sum(
            1
            for key, impressed_at in earliest_impression.items()
            if key in earliest_add and earliest_add[key] > impressed_at
        )

    total = len(earliest_impression)
    return {
        "impressions": total,
        "converted": converted,
        "rate": (converted / total) if total else None,
    }


async def multi_item_completion_rate(connection_id: str, days: int = 30) -> dict:
    """Of the second-item proposals queued by a multi-item add-in-one-message
    (`engine/api/chat.py`'s `_find_second_item`, tagged
    `detail.source == "multi_item_add"` on their `PROPOSAL_CREATED` event),
    how many were actually completed (`PROPOSAL_EXECUTED`, joined on the
    same `proposal_id`) - the named figure CLAUDE.md's value bar requires
    for this feature: before it, a second item named in the same message
    was silently dropped with no queue at all; now it is reachable in one
    tap, and this is what lets anyone actually check whether shoppers take
    that tap.

    Same standing caveat as `shortlist_engagement`: instrumentation, not
    proof - sample sizes in practice are small and mostly test traffic
    during development. Do not read a rate out of this and call it
    business evidence without a real sample first.
    """
    since = datetime.now(UTC) - timedelta(days=days)
    async with session_scope() as db:
        created_rows = (
            await db.execute(
                select(ShopperEvent.detail).where(
                    ShopperEvent.connection_id == connection_id,
                    ShopperEvent.event_type == EventType.PROPOSAL_CREATED,
                    ShopperEvent.created_at >= since,
                )
            )
        ).all()
        queued_ids = {
            row.detail.get("proposal_id")
            for row in created_rows
            if row.detail and row.detail.get("source") == "multi_item_add"
            and row.detail.get("proposal_id")
        }
        if not queued_ids:
            return {"queued": 0, "completed": 0, "rate": None}

        executed_rows = (
            await db.execute(
                select(ShopperEvent.detail).where(
                    ShopperEvent.connection_id == connection_id,
                    ShopperEvent.event_type == EventType.PROPOSAL_EXECUTED,
                    ShopperEvent.created_at >= since,
                )
            )
        ).all()
        executed_ids = {
            row.detail.get("proposal_id") for row in executed_rows if row.detail
        }

    completed = len(queued_ids & executed_ids)
    total = len(queued_ids)
    return {
        "queued": total,
        "completed": completed,
        "rate": (completed / total) if total else None,
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
