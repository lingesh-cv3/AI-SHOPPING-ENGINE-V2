"""Idempotency.

Until now the execution service carried comments claiming a retry could not
double-charge. It could. execute_case called twice ran twice, and with Kettle &
Bloom able to actually move money, that stopped being theoretical.

This module makes the claim true.

The key is derived from the case, not generated per attempt. A random key per call
would guarantee every retry looked new, which is precisely the failure the mechanism
exists to prevent. Deriving it from the case id means the second attempt carries the
same key as the first, and both we and the platform can recognise it.

Two layers, because one is not enough. The platform's own idempotency is
authoritative where it exists, but not every platform has one and we cannot verify
the ones that claim to. So we keep our own ledger, checked before we call out at
all.

Only money-touching actions are guarded. Running a search twice is harmless and a
ledger row for it would be noise. The guard goes where a repeat has a cost.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select

from shared.models import ACTION_RISK_PROPERTIES, ActionType

from .models import ExecutionAttempt, OrderLine
from .session import session_scope

#: How long a completed attempt is remembered. Long enough to cover any plausible
#: retry, short enough that the table does not grow without bound.
RETENTION = timedelta(days=7)


def key_for(
    case_id: str, action_type: str, parameters: dict[str, Any] | None = None
) -> str:
    """The idempotency key for one action on one case.

    Deterministic: the same case and action always produce the same key, which is
    what makes a retry recognisable as a retry.

    Parameters are folded in because the same action on the same case with different
    parameters is a different intent. Applying FIRSTBAG and then applying BREWKIT500
    to the same cart are two things, not one thing twice.
    """
    payload = json.dumps(parameters or {}, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(f"{case_id}|{action_type}|{payload}".encode()).hexdigest()
    return f"idem_{digest[:32]}"


#: Actions that change something the shopper can see, beyond the money ones.
#:
#: The guard originally covered financial actions only, on the reasoning that a
#: repeated search costs nothing. True, but too narrow: a repeated add-to-cart costs
#: the shopper a second bag of coffee they did not ask for, which they discover at
#: checkout. Anything that mutates state a shopper looks at belongs here.
_MUTATING: frozenset[ActionType] = frozenset(
    {
        ActionType.ADD_TO_CART,
        ActionType.REMOVE_CART_LINE,
        ActionType.UPDATE_CART_QUANTITY,
    }
)


def guarded(action_type: ActionType) -> bool:
    """Whether this action needs the guard. Money-touching actions only."""
    if action_type in _MUTATING:
        return True
    props = ACTION_RISK_PROPERTIES.get(action_type)
    return bool(props and props.financial)


async def claim(
    *,
    connection_id: str,
    case_id: str,
    action_type: str,
    idempotency_key: str,
) -> dict | None:
    """Claim the right to execute, or return the previous result.

    None means this is the first attempt and execution should proceed. A dict means
    this key has been seen before and the caller should return that instead.

    The row is written before the platform call, not after. Writing after leaves a
    window where a crash mid-execution loses the record and the retry charges again.
    Writing first leaves an IN_FLIGHT row, which is recoverable.
    """
    async with session_scope() as db:
        existing = await db.get(ExecutionAttempt, idempotency_key)

        if existing is not None:
            if existing.connection_id != connection_id:
                # Same key, different merchant. Should be impossible given the key
                # derivation, but returning another tenant's result would be far
                # worse than refusing.
                return {
                    "state": "CONFLICT",
                    "summary": "that idempotency key belongs to another connection",
                }
            return {
                "state": existing.state,
                "succeeded": existing.succeeded,
                "summary": existing.summary,
                "result": existing.result,
                "first_attempted_at": existing.created_at.isoformat(),
            }

        db.add(
            ExecutionAttempt(
                idempotency_key=idempotency_key,
                connection_id=connection_id,
                case_id=case_id,
                action_type=action_type,
                state="IN_FLIGHT",
            )
        )
    return None


async def complete(
    idempotency_key: str,
    *,
    succeeded: bool,
    summary: str,
    result: dict | None = None,
) -> None:
    """Record how the attempt ended.

    An IN_FLIGHT row that never completes means the process died mid-execution. It
    stays IN_FLIGHT deliberately: a later attempt reads it and knows the platform may
    or may not have acted, which is a situation a person should look at rather than a
    machine guess.
    """
    async with session_scope() as db:
        attempt = await db.get(ExecutionAttempt, idempotency_key)
        if attempt is None:
            return
        attempt.state = "DONE"
        attempt.succeeded = succeeded
        attempt.summary = summary[:500]
        attempt.result = result or {}
        attempt.completed_at = datetime.now(UTC)


async def purge_old() -> int:
    """Drop attempts past the retention window. Returns how many went."""
    cutoff = datetime.now(UTC) - RETENTION
    async with session_scope() as db:
        rows = await db.execute(
            select(ExecutionAttempt).where(ExecutionAttempt.created_at < cutoff)
        )
        stale = list(rows.scalars())
        for row in stale:
            await db.delete(row)
        return len(stale)


# ---------------------------------------------------------------------------
# A basket is paid for once
# ---------------------------------------------------------------------------
#
# Everything above guards one action on one case. This guards one basket across
# every route that can charge it, which is a different question and was not being
# asked by anybody.
#
# What went wrong: the key handed to the platform was derived from the cart and
# the card. The same card twice was recognised as a retry, so "a paid cart is
# never chargeable again" looked true and was only ever true of a repeated tap on
# one card. A different card was a different key, and the same basket bought three
# separate orders at the full amount on both platforms.
#
# The card cannot come out of that key. A declined purchase is stored under it
# too, so a cart-only key would replay the decline forever and a shopper whose
# card was refused could never pay with another one - which is the recovery this
# whole system exists for. So the platform key keeps the card, and the engine is
# what knows a basket has already been bought.


def cart_payment_key(connection_id: str, cart_id: str) -> str:
    """The ledger key for paying one basket.

    The cart and nothing else. Not the card, not the attempt - the question this
    answers is "has this basket been bought", and the answer must not depend on
    which card somebody is holding when they ask.
    """
    digest = hashlib.sha256(f"{connection_id}|{cart_id}".encode()).hexdigest()
    return f"paid_{digest[:32]}"


async def is_paid(connection_id: str, cart_id: str) -> bool:
    """Whether this basket has already been bought.

    The ledger's own answer, asked from outside a payment attempt - by a route
    that wants to refuse changing a cart rather than charging one. Lines could be
    added to a cart, and quantities changed, after the order for it already
    existed: the REST cart routes and the chat/tap execution path both call the
    adapter directly and neither one asked whether the basket they were about to
    change had already been paid for. A declined or in-flight attempt does not
    count - only a completed, successful one locks the basket against further
    changes, which matches begin_payment's own idea of "bought".
    """
    key = cart_payment_key(connection_id, cart_id)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        return bool(row is not None and row.state == "DONE" and row.succeeded)


async def begin_payment(connection_id: str, cart_id: str) -> dict | None:
    """Take the right to charge this basket, or say why not.

    None means charge it. A dict means do not, and carries `already_paid` for a
    basket that is bought and `in_flight` for one somebody is buying right now.

    The in-flight case is the race the ledger exists for: two taps on two cards
    arriving together both read "not paid yet" and both charge. Claiming the row
    before the platform call means the second one loses, which is the whole reason
    this is written first rather than after.
    """
    key = cart_payment_key(connection_id, cart_id)

    async with session_scope() as db:
        existing = await db.get(ExecutionAttempt, key)

        if existing is not None:
            if existing.state == "DONE" and existing.succeeded:
                return {
                    "already_paid": True,
                    "order_id": (existing.result or {}).get("order_id"),
                    "summary": existing.summary,
                }
            if existing.state == "IN_FLIGHT":
                return {"in_flight": True}

            # DONE and not succeeded. A decline is not a purchase, so the basket is
            # still buyable - which is the entire point of the recovery path. The
            # row is reused rather than left behind to block them.
            existing.state = "IN_FLIGHT"
            existing.succeeded = None
            existing.completed_at = None
            return None

        db.add(
            ExecutionAttempt(
                idempotency_key=key,
                connection_id=connection_id,
                case_id=cart_id[:40],
                action_type="CHECKOUT",
                state="IN_FLIGHT",
            )
        )

    return None


async def payment_settled(
    connection_id: str,
    cart_id: str,
    *,
    succeeded: bool,
    order_id: str | None,
    summary: str,
    amount: str | None = None,
    currency: str | None = None,
    lines: list[dict] | None = None,
) -> None:
    """Record how the charge ended.

    A success locks the basket for good. A decline leaves it buyable, because the
    shopper is about to be offered another way to pay and refusing them then would
    turn a recoverable sale into a lost one.

    `lines` - one dict per product on the order (product_id, product_name,
    quantity, unit_price, line_total) straight from the adapter's own
    `Order.lines` (or a recovery's `PaymentRecoveryResult.order.lines`) -
    written as `OrderLine` rows, and only ever for a *succeeded*, priced
    order, the same gate `amount_paid` already uses. A decline or an
    unpriced/legacy row writes no product data, which is honest: there is
    nothing completed to attribute it to.
    """
    key = cart_payment_key(connection_id, cart_id)

    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is None:
            return
        row.state = "DONE"
        row.succeeded = succeeded
        row.summary = summary[:500]
        result: dict = {}
        if order_id:
            result["order_id"] = order_id
        # Only a successful, priced order contributes a figure - a decline
        # settles the row (so the ledger sees the attempt) but must never be
        # counted as sales, and this is the one place `total_sales` reads
        # its numbers from.
        if succeeded and amount is not None:
            result["amount_paid"] = amount
            if currency:
                result["currency"] = currency
        row.result = result
        row.completed_at = datetime.now(UTC)

        if succeeded and order_id and lines:
            for i, line in enumerate(lines):
                product_id = line.get("product_id")
                if not product_id:
                    continue
                row_id = f"{key}:{i}"
                order_line = await db.get(OrderLine, row_id)
                if order_line is None:
                    order_line = OrderLine(row_id=row_id)
                    db.add(order_line)
                order_line.connection_id = connection_id
                order_line.order_id = order_id
                order_line.cart_id = cart_id
                order_line.product_id = str(product_id)
                order_line.product_name = str(line.get("product_name") or product_id)
                order_line.quantity = int(line.get("quantity") or 0)
                order_line.unit_price = (
                    str(line["unit_price"]) if line.get("unit_price") is not None else None
                )
                order_line.line_total = (
                    str(line["line_total"]) if line.get("line_total") is not None else None
                )
                order_line.currency = line.get("currency") or currency


async def release_payment(connection_id: str, cart_id: str) -> None:
    """Give the basket back after a charge that never happened.

    For the platform refusing the call outright - an empty basket, a network
    failure. Nothing was attempted, so leaving a row behind would lock somebody out
    of a basket over an error that was not theirs.
    """
    key = cart_payment_key(connection_id, cart_id)

    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is not None and row.state == "IN_FLIGHT":
            await db.delete(row)


async def forget_payment(connection_id: str, cart_id: str) -> None:
    """Discard any record of this cart having been paid for.

    Cart ids are sequential and the platform's counter lives in its own memory,
    so a restart reissues BSK00001 to a shopper who has never seen it - the same
    fact that made db.owners.take() necessary for ownership. This closes the
    identical hole here: without it, a brand new empty basket handed the same id
    a paid basket once had is told it is already bought, with no order to show
    for it because the order from before the restart is gone too. That is worse
    than the double charge this ledger exists to prevent - it refuses a
    shopper's first, legitimate payment.

    Called at the one moment the answer is not a guess: create_cart, where the
    platform has just told us this id is fresh. Nothing about a payment can be
    racing a cart that has not been created yet, so there is no IN_FLIGHT row to
    protect here the way release_payment protects one.
    """
    key = cart_payment_key(connection_id, cart_id)

    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is not None:
            await db.delete(row)
