"""Tell people when a payment outcome could not be confirmed - once, with an audit reference, and nothing sensitive.

A basket parked as UNKNOWN_OUTCOME (`engine/db/idempotency.py`) is held between "maybe charged" and "not charged". Until somebody
looks, nobody knows it is there. This module raises one merchant task per unknown-outcome EPISODE (kind PAYMENT_UNRESOLVED, shown in
the merchant's Merchant Tasks) and counts the held baskets for operators (`ops_stats.unresolved_payments`, plus the operator list).

- Deduplicated twice: the ledger row records the episode it already alerted for, and the task's unique (connection, kind, subject)
  constraint refuses a second row for the same episode. An episode starts when a basket first becomes unknown and survives same-key
  retries, so repeated taps, retries, sweeps and restarts raise one alert; a later, separate unknown outcome raises a new one.
- Failures are recorded (state "failed", with a try count on the row) and retried by the periodic sweep, never raised to a shopper's tap.
- The task closes itself (RESOLVED, decided_by "system: ...") when the ledger leaves UNKNOWN_OUTCOME. It is informational: dismissing it
  never changes the basket - only reconciliation or an operator's resolution does.
- Content is a reference (a hash of the connection and basket), the basket id, the reason CODE and when it began. Never a card number,
  an amount, an order or shopper identity, an idempotency key, or any platform text (invariant 5).
- Tenant isolation: the task is written for the ledger row's own connection only.
"""

from __future__ import annotations

import logging

from engine.db import idempotency as ledger
from engine.db import repository

logger = logging.getLogger(__name__)

#: A failed alert is retried by each sweep up to this many times, then left to the operator list (which never depends on it).
MAX_ALERT_TRIES = 5

CLOSED_BY = "system: payment resolved in the ledger"

#: Rows read per page by the sweep (a real client can have more unresolved baskets than one page).
_PAGE = 200
#: Upper bound on pages per sweep, so one sweep is always bounded; the next sweep continues from the start, and the alert
#: the sweep misses is still counted in ops_stats and listed by the operator route.
_MAX_PAGES = 50

#: Ledger states in which an alert stays open: unknown, or a same-key retry of the unknown payment is in flight.
_OPEN_STATES = (ledger.UNKNOWN_OUTCOME, ledger.IN_FLIGHT)


def payment_ref(connection_id: str, cart_id: str) -> str:
    """A stable, non-reversible reference for audit: the ledger key of the basket (a hash of connection and cart)."""
    return ledger.cart_payment_key(connection_id, cart_id)


def _episode(row: dict) -> str | None:
    """The unknown-outcome episode this row is in: a random id fixed when it first became unknown (survives same-key retries).

    A row written before episodes existed falls back to its start time, which is only ever used for that legacy case.
    """
    if row.get("episode"):
        return str(row["episode"])
    started = row.get("first_unknown_at") or row.get("unknown_at")
    return f"t{int(started.timestamp())}" if started else None


async def notify(connection_id: str, cart_id: str) -> str:
    """Raise the merchant task for this basket's unknown-outcome episode if it has not been raised. Never raises.

    Returns "sent", "already", "not_unknown", "gave_up" or "failed".
    """
    try:
        row = await ledger.payment_row(connection_id, cart_id)
        if row is None or row["state"] != ledger.UNKNOWN_OUTCOME:
            return "not_unknown"
        episode = _episode(row)
        if episode is None:
            return "not_unknown"
        alert = row["alert"]
        if alert.get("episode") == episode:
            if alert.get("state") == "sent":
                return "already"
            if int(alert.get("tries") or 0) >= MAX_ALERT_TRIES:
                return "gave_up"

        ref = payment_ref(connection_id, cart_id)
        try:
            created = await repository.upsert_payment_alert(
                connection_id,
                f"{ref}:{episode}",
                "A shopper's payment outcome could not be confirmed. The basket is on hold and the shopper has been asked not to pay again.",
                {
                    "payment_ref": ref,
                    "cart_id": cart_id,
                    "reason": row["reason"],
                    "since": row["first_unknown_at"].isoformat() if row["first_unknown_at"] else None,
                    "what_to_do": "CV3 checks with the platform automatically. If you can see the order in your back office, tell CV3 operations; do not refund or re-charge from here.",
                },
            )
        except Exception:  # noqa: BLE001 - recorded and retried by the sweep; the shopper's tap must not fail over a notification
            logger.exception("could not raise the unresolved-payment alert for %s", ref)
            await ledger.record_alert(connection_id, cart_id, episode=episode, state="failed", token=row["token"])
            return "failed"
        await ledger.record_alert(connection_id, cart_id, episode=episode, state="sent", token=row["token"])
        if created:
            logger.warning("unresolved payment alert raised: %s (%s) reason=%s", ref, connection_id, row["reason"])
        return "sent" if created else "already"
    except Exception:  # noqa: BLE001
        logger.exception("payment alert bookkeeping failed")
        return "failed"


async def close_for(connection_id: str, cart_id: str) -> int:
    """Close the basket's open alerts once its ledger row is no longer UNKNOWN_OUTCOME. Never raises."""
    try:
        row = await ledger.payment_row(connection_id, cart_id)
        if row is not None and row["state"] in _OPEN_STATES:
            return 0  # still unresolved, or a retry of it is in flight (it may end unknown again)
        return await repository.close_payment_alerts(connection_id, f"{payment_ref(connection_id, cart_id)}:", decided_by=CLOSED_BY)
    except Exception:  # noqa: BLE001
        logger.exception("could not close the unresolved-payment alert")
        return 0


async def sync(connection_id: str, cart_id: str) -> str:
    """Make the alert match the ledger: raise it while unknown, close it once resolved. Never raises."""
    try:
        row = await ledger.payment_row(connection_id, cart_id)
        if row is not None and row["state"] == ledger.UNKNOWN_OUTCOME:
            return await notify(connection_id, cart_id)
        await close_for(connection_id, cart_id)
        return "closed_or_none"
    except Exception:  # noqa: BLE001
        logger.exception("payment alert sync failed")
        return "failed"


async def sweep() -> dict:
    """Periodic: alert every unknown basket that has no alert yet (stale/orphaned rows, earlier failures), close alerts whose basket resolved."""
    out = {"raised": 0, "closed": 0, "failed": 0}
    try:
        for page in range(_MAX_PAGES):
            total, rows = await ledger.unresolved_payments(limit=_PAGE, offset=page * _PAGE)
            for row in rows:
                result = await notify(row["connection_id"], row["cart_id"])
                if result == "sent":
                    out["raised"] += 1
                elif result == "failed":
                    out["failed"] += 1
            if (page + 1) * _PAGE >= total:
                break
        for page in range(_MAX_PAGES):
            alerts = await repository.open_payment_alerts(limit=_PAGE, offset=page * _PAGE)
            for connection_id, subject, cart_id in alerts:
                ref, _, episode = subject.partition(":")
                row = await ledger.payment_row(connection_id, cart_id) if cart_id else None
                # stale when its basket is settled or gone, or has moved on to a different episode; an unknown row, or the
                # same episode's retry in flight, keeps its alert
                if row is None or row["state"] not in _OPEN_STATES or _episode(row) != episode:
                    out["closed"] += await repository.close_payment_alerts(connection_id, f"{ref}:{episode}", decided_by=CLOSED_BY)
            if len(alerts) < _PAGE:
                break
    except Exception:  # noqa: BLE001
        logger.exception("payment alert sweep failed")
    return out
