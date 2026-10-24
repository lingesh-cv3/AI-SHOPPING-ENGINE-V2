"""Evidence-based reconciliation of payments whose outcome is unknown.

When a platform call ends in a timeout, a 5xx, a crash or anything else that leaves "did it charge?" unanswered, the
basket's ledger row becomes UNKNOWN_OUTCOME (`engine/db/idempotency.py`). This module is how such a row ends - and the only
ways it can end are:

- the platform shows a PAID order for the basket          -> DONE (paid), with the platform's order id;
- the platform answers COMPLETELY, shows no order (or only
  unpaid ones), and the settle window has passed           -> DONE (declined: "confirmed not charged"), buyable again;
- a named operator resolves it (`resolve`), subject to what the platform says.

Everything else stays UNKNOWN_OUTCOME and blocked. Nothing here guesses: a lookup that fails, is partial, is not offered by the
platform (Kettle has none) or answers too early is "inconclusive", never "absent". Nothing in this module can move money - it
only reads from a platform and writes to the ledger.

Tenant isolation: every call is for ONE (connection_id, cart_id) and uses the adapter of THAT connection. Statuses and reasons
recorded in the ledger are fixed codes, never platform text (invariant 5).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

from engine import db
from engine.db import idempotency as ledger
from shared.models import Order
from shared.models.commerce import PaymentStatus

logger = logging.getLogger(__name__)

#: A platform lookup that has not answered in this many seconds is inconclusive.
LOOKUP_TIMEOUT_SECONDS = 20.0

_PAID = frozenset({PaymentStatus.CAPTURED, PaymentStatus.AUTHORIZED})
_UNPAID_FINAL = frozenset({PaymentStatus.DECLINED, PaymentStatus.FAILED, PaymentStatus.NOT_ATTEMPTED})


def order_settlement_args(order: Order | None, *, succeeded: bool) -> dict:
    """The ledger fields describing an order, in one place (both pay routes and the reconciler used to each build this)."""
    ok = bool(succeeded and order)
    paid = order.amount_paid if order else None
    return {
        "order_id": order.order_id if order else None,
        "amount": str(paid.amount) if ok and paid else None,
        "currency": paid.currency if ok and paid else None,
        "lines": [
            {
                "product_id": line.product_id,
                "product_name": line.title,
                "quantity": line.quantity,
                "unit_price": str(line.unit_price.amount) if line.unit_price else None,
                "line_total": str(line.line_total.amount) if line.line_total else None,
                "currency": line.unit_price.currency if line.unit_price else None,
            }
            for line in order.lines
        ]
        if ok
        else None,
    }


def _is_paid(order: Order) -> bool:
    return order.payment_status in _PAID


def _is_unpaid_final(order: Order) -> bool:
    return order.payment_status in _UNPAID_FINAL


async def _lookup(adapter, cart_id: str, keys: list[str]) -> tuple[str, list[Order]]:
    """(status, orders): FOUND / ABSENT / UNKNOWN. Any failure, partial answer or missing capability is UNKNOWN."""
    lookup = getattr(adapter, "lookup_checkout", None)
    if lookup is None:
        return "NO_LOOKUP", []
    try:
        found = await asyncio.wait_for(lookup(cart_id, keys), LOOKUP_TIMEOUT_SECONDS)
    except Exception:  # noqa: BLE001 - any failure, including a platform error, is inconclusive
        logger.warning("checkout lookup failed", exc_info=True)
        return "LOOKUP_FAILED", []
    return str(found.status), list(found.orders)


async def finish_paid_elsewhere(connection_id: str, cart_id: str, order: Order, *, token: str | None, resolution: dict | None = None) -> bool:
    """A paid order exists for this basket: settle the row and do what the pay route does after a success.

    Ownership is recorded for the CART's owner only - a first touch never creates ownership, and an existing order owner is
    never overwritten. The confirmation mail is not sent (no shopper address is in scope here) and no purchase event is
    recorded (no session is known); both are stated limits.
    """
    args = order_settlement_args(order, succeeded=True)
    settled = await ledger.payment_settled(
        connection_id, cart_id, succeeded=True, summary=f"paid {order.order_id} (confirmed with the platform)",
        token=token, from_unknown=True, resolution=resolution, **args,
    )
    if not settled:
        return False
    try:
        cart_owner = await db.owners.owner_of(connection_id, db.owners.CART, cart_id)
        if cart_owner and await db.owners.owner_of(connection_id, db.owners.ORDER, order.order_id) is None:
            await db.owners.take(connection_id, db.owners.ORDER, order.order_id, cart_owner)
        await db.resolve_unresolved_payment_cases_for_cart(
            connection_id, cart_id, amount=args["amount"], currency=args["currency"]
        )
    except Exception:  # noqa: BLE001 - the money fact is settled; the follow-ups are best effort
        logger.exception("could not finish follow-ups for a reconciled payment")
    return True


def _age_seconds(row: dict) -> float:
    unknown_at = row.get("unknown_at")
    return (datetime.now(UTC) - unknown_at).total_seconds() if unknown_at else 0.0


async def reconcile(adapter, connection_id: str, cart_id: str, *, force: bool = False) -> dict:
    """Try to resolve one UNKNOWN_OUTCOME basket with evidence. Never raises. Returns {"status": <fixed code>}."""
    try:
        row = await ledger.payment_row(connection_id, cart_id)
        if row is None or row["state"] != ledger.UNKNOWN_OUTCOME:
            return {"status": "not_unknown"}

        if not force:
            at = ledger._parse(row["recon"].get("at"))
            if at is not None and (datetime.now(UTC) - at).total_seconds() < ledger.RECON_THROTTLE_SECONDS:
                return {"status": "throttled"}

        age = _age_seconds(row)
        past_window = age >= ledger.settle_window()
        status, orders = await _lookup(adapter, cart_id, row["attempt_keys"])
        token = row["token"]

        if status == "FOUND":
            paid = [o for o in orders if _is_paid(o)]
            if paid:
                # More than one paid order for one basket means the platform already charged twice; the basket is still
                # bought (settle on the first), the duplicates are recorded for a person and logged loudly.
                first, *dupes = paid
                if dupes:
                    logger.error("PAYMENT DUPLICATE: %s/%s has %d paid platform orders", connection_id, cart_id, len(paid))
                if await finish_paid_elsewhere(connection_id, cart_id, first, token=token):
                    return {"status": "paid", "order_id": first.order_id, "duplicates": len(dupes)}
                return {"status": "moved"}
            if orders and all(_is_unpaid_final(o) for o in orders) and past_window:
                if await ledger.payment_settled(
                    connection_id, cart_id, succeeded=False, order_id=None,
                    summary="confirmed not charged (only unpaid orders on the platform)", token=token, from_unknown=True,
                ):
                    return {"status": "cleared"}
                return {"status": "moved"}
            outcome = "found_unpaid_too_early" if orders and all(_is_unpaid_final(o) for o in orders) else "found_status_unclear"
        elif status == "ABSENT":
            if past_window:
                if await ledger.payment_settled(
                    connection_id, cart_id, succeeded=False, order_id=None,
                    summary="confirmed not charged (no order on the platform)", token=token, from_unknown=True,
                ):
                    return {"status": "cleared"}
                return {"status": "moved"}
            outcome = "absent_too_early"
        else:
            outcome = {"NO_LOOKUP": "no_lookup", "LOOKUP_FAILED": "lookup_failed"}.get(status, "lookup_inconclusive")

        await ledger.record_reconciliation(connection_id, cart_id, status=outcome, manual=past_window, token=token)
        return {"status": outcome}
    except Exception:  # noqa: BLE001 - reconciliation must never break a tap or the sweep
        logger.exception("payment reconciliation failed")
        return {"status": "error"}


async def resolve(
    adapter, connection_id: str, cart_id: str, *, resolution: str, order_id: str | None, note: str | None, operator_id: str = "operator"
) -> dict:
    """An operator's decision on an UNKNOWN_OUTCOME basket: "paid" (with the order id) or "not_charged".

    The platform's evidence outranks the operator: `not_charged` is refused while the platform shows a paid order, and `paid`
    is refused when the platform answers completely and shows none or does not show that order. With no lookup (or an
    inconclusive one) the operator's call stands and is recorded as asserted. CAS-protected: a row that moved is not touched.
    """
    row = await ledger.payment_row(connection_id, cart_id)
    if row is None or row["state"] != ledger.UNKNOWN_OUTCOME:
        return {"status": "not_found"}

    status, orders = await _lookup(adapter, cart_id, row["attempt_keys"])
    paid = [o for o in orders if _is_paid(o)] if status == "FOUND" else []
    audit = {"by": operator_id, "at": ledger._iso(), "resolution": resolution, "note": (note or "")[:300]}
    token = row["token"]

    if resolution == "not_charged":
        if paid:
            return {"status": "refused_paid_on_platform", "order_id": paid[0].order_id}
        if _age_seconds(row) < ledger.settle_window():
            # A request the platform is still holding can land after this decision; a person gets the same margin the machine does.
            return {"status": "refused_too_early"}
        audit["asserted"] = status not in ("ABSENT",)
        done = await ledger.payment_settled(
            connection_id, cart_id, succeeded=False, order_id=None, summary="resolved by an operator: not charged",
            token=token, from_unknown=True, resolution=audit,
        )
        return {"status": "resolved" if done else "moved"}

    if resolution == "paid":
        if status == "ABSENT":
            return {"status": "refused_absent_on_platform"}
        if status == "FOUND":
            match = next((o for o in paid if not order_id or o.order_id == order_id), None)
            if match is None:
                return {"status": "refused_order_not_found_on_platform"}
            audit["asserted"] = False
            return {"status": "resolved" if await finish_paid_elsewhere(connection_id, cart_id, match, token=token, resolution=audit) else "moved", "order_id": match.order_id}
        if not order_id:
            return {"status": "order_id_required"}
        audit["asserted"] = True
        done = await ledger.payment_settled(
            connection_id, cart_id, succeeded=True, order_id=order_id, summary=f"paid {order_id} (operator-asserted)",
            token=token, from_unknown=True, resolution=audit,
        )
        return {"status": "resolved" if done else "moved", "order_id": order_id}

    return {"status": "bad_resolution"}


async def sweep(registry) -> dict:
    """Periodic: stale IN_FLIGHT rows become UNKNOWN_OUTCOME (never released), then each UNKNOWN row is reconciled."""
    result = {"stale": 0, "checked": 0, "paid": 0, "cleared": 0}
    try:
        result["stale"] = await ledger.stale_in_flight_to_unknown()
        _total, rows = await ledger.unresolved_payments(limit=200)
        for row in rows:
            adapter = registry.adapter_for(row["connection_id"])
            if adapter is None:
                continue
            outcome = await reconcile(adapter, row["connection_id"], row["cart_id"])
            result["checked"] += 1
            if outcome["status"] == "paid":
                result["paid"] += 1
            elif outcome["status"] == "cleared":
                result["cleared"] += 1
    except Exception:  # noqa: BLE001
        logger.exception("payment sweep failed")
    return result
