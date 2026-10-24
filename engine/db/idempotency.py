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
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from shared.models import ACTION_RISK_PROPERTIES, ActionType

from .models import ExecutionAttempt, OrderLine
from .session import session_scope

logger = logging.getLogger(__name__)

#: Ledger states. UNKNOWN_OUTCOME is the one that matters for money: the platform may or may not have acted, nobody knows, and the
#: basket stays closed until evidence (a platform lookup) or a named person says which.
IN_FLIGHT = "IN_FLIGHT"
DONE = "DONE"
UNKNOWN_OUTCOME = "UNKNOWN_OUTCOME"


def _seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, ""))
        return value if value > 0 else default
    except ValueError:
        return default


def call_deadline() -> float:
    """Hard ceiling on one platform payment call (seconds). The HTTP client's 10 s timeout is per phase, not total."""
    return _seconds("PAYMENT_CALL_DEADLINE_SECONDS", 60.0)


def stale_after() -> float:
    """An IN_FLIGHT row older than this has no live handler behind it: twice the call deadline."""
    return 2 * call_deadline()


def settle_window() -> float:
    """How long after a row became UNKNOWN_OUTCOME an absence on the platform counts as conclusive (and a same-key retry is allowed).

    A request held by the platform can still land after the engine gave up on it; this bound is the margin for that, and
    "confirmed not charged" is only as good as it.
    """
    return _seconds("PAYMENT_SETTLE_WINDOW_SECONDS", 120.0)


#: How long before a blocked tap may trigger another platform lookup for the same basket.
RECON_THROTTLE_SECONDS = 15.0

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
        # Never an unresolved payment: an IN_FLIGHT or UNKNOWN_OUTCOME row is the only record a basket may have been charged.
        rows = await db.execute(
            select(ExecutionAttempt).where(
                ExecutionAttempt.created_at < cutoff,
                ExecutionAttempt.state.notin_([IN_FLIGHT, UNKNOWN_OUTCOME]),
            )
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


_ANY = object()


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(moment: datetime | None = None) -> str:
    return (moment or _now()).isoformat()


def _parse(value: object) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _snapshot(row: ExecutionAttempt) -> dict:
    """What callers may know about a ledger row. Reason codes and times only - never platform detail."""
    res = dict(row.result or {})
    return {
        "connection_id": row.connection_id,
        "cart_id": row.case_id,
        "state": row.state,
        "succeeded": row.succeeded,
        "summary": row.summary,
        "order_id": res.get("order_id"),
        "token": res.get("attempt_token"),
        "attempt_keys": list(res.get("attempt_keys") or []),
        "reason": res.get("reason"),
        "unknown_at": _parse(res.get("unknown_at")),
        "started_at": _parse(res.get("started_at")) or _aware(row.created_at),
        "recon": dict(res.get("recon") or {}),
        "manual": bool(res.get("manual")),
        "retry_of_unknown": bool(res.get("retry_of_unknown")),
        "operator": dict(res.get("operator") or {}),
        "first_unknown_at": _parse(res.get("first_unknown_at")) or _parse(res.get("unknown_at")),
        "episode": res.get("episode"),
        "alert": dict(res.get("alert") or {}),
    }


async def _cas(
    db,
    key: str,
    *,
    states: list[str],
    values: dict,
    token: object = _ANY,
    succeeded: object = _ANY,
) -> bool:
    """Move one row only if it is still in the state (and claim) the caller read. True means this caller won the move."""
    stmt = update(ExecutionAttempt).where(
        ExecutionAttempt.idempotency_key == key, ExecutionAttempt.state.in_(states)
    )
    if token is not _ANY:
        stmt = stmt.where(ExecutionAttempt.result["attempt_token"].as_string() == token)
    if succeeded is not _ANY:
        stmt = stmt.where(ExecutionAttempt.succeeded.is_(succeeded))
    result = await db.execute(stmt.values(**values))
    return result.rowcount == 1


def _token_pred(token: str | None) -> object:
    """A claim with no token (a row written before tokens existed) is matched on state alone."""
    return token if token else _ANY


async def payment_row(connection_id: str, cart_id: str) -> dict | None:
    """The basket's ledger row as a snapshot, or None."""
    key = cart_payment_key(connection_id, cart_id)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        return _snapshot(row) if row is not None else None


async def payment_lock(connection_id: str, cart_id: str) -> str | None:
    """Why this basket may not be changed or paid right now: "paid", "in_flight", "unknown", or None when it is free.

    A declined attempt does not lock (the shopper is about to be offered another way to pay). Cart mutations are refused for
    every other state, because the basket either has been bought or may have been.
    """
    row = await payment_row(connection_id, cart_id)
    if row is None:
        return None
    if row["state"] == UNKNOWN_OUTCOME:
        return "unknown"
    if row["state"] == IN_FLIGHT:
        return "in_flight"
    if row["state"] == DONE and row["succeeded"]:
        return "paid"
    return None


async def is_paid(connection_id: str, cart_id: str) -> bool:
    """Whether this basket has already been bought (a completed, successful attempt)."""
    return await payment_lock(connection_id, cart_id) == "paid"


def _claim_result(prev: dict, attempt_key: str | None, *, retry_of_unknown: bool) -> tuple[str, dict]:
    token = uuid.uuid4().hex
    keys = list(prev.get("attempt_keys") or [])
    if attempt_key and attempt_key not in keys:
        keys.append(attempt_key)
    res: dict = {
        "attempt_token": token,
        "started_at": _iso(),
        "attempt_keys": keys,
        "retry_of_unknown": retry_of_unknown,
    }
    if retry_of_unknown:
        # an unknown outcome's history survives a same-key retry; a fresh claim after a decline starts clean
        for field in ("reason", "unknown_at", "first_unknown_at", "recon", "episode", "alert"):
            if prev.get(field) is not None:
                res[field] = prev[field]
        res.setdefault("first_unknown_at", prev.get("unknown_at"))
    return token, res


async def begin_payment(connection_id: str, cart_id: str, *, attempt_key: str | None = None) -> dict:
    """Take the right to charge this basket, or say why not. Every move is a compare-and-set.

    Returns one of:
      {"claimed": True, "token": ...}      charge it (carry the token to settle / mark unknown / release)
      {"already_paid": True, "order_id"}   bought
      {"in_flight": True}                  somebody is buying it right now
      {"unknown": True, "row": {...}}      a previous attempt's outcome is unresolved: do NOT charge

    An IN_FLIGHT row older than `stale_after()` has no live handler behind it (the process died or the call was lost), and a
    stale row is never released: it becomes UNKNOWN_OUTCOME, because the platform may have acted.
    """
    key = cart_payment_key(connection_id, cart_id)

    for _ in range(4):
        async with session_scope() as db:
            row = await db.get(ExecutionAttempt, key)

            if row is None:
                token, res = _claim_result({}, attempt_key, retry_of_unknown=False)
                db.add(
                    ExecutionAttempt(
                        idempotency_key=key,
                        connection_id=connection_id,
                        case_id=cart_id[:40],
                        action_type="CHECKOUT",
                        state=IN_FLIGHT,
                        result=res,
                    )
                )
                try:
                    await db.flush()
                except IntegrityError:
                    await db.rollback()
                    continue  # someone else claimed it between the read and the insert; read again
                return {"claimed": True, "token": token}

            snap = _snapshot(row)

            if row.state == DONE and row.succeeded:
                return {"already_paid": True, "order_id": snap["order_id"], "summary": row.summary}

            if row.state == UNKNOWN_OUTCOME:
                return {"unknown": True, "row": snap}

            if row.state == IN_FLIGHT:
                started = snap["started_at"]
                if started is not None and (_now() - started).total_seconds() > stale_after():
                    res = dict(row.result or {})
                    res.update({"reason": "stale_in_flight", "unknown_at": _iso(), "manual": False})
                    res.setdefault("first_unknown_at", res["unknown_at"])
                    res.setdefault("episode", uuid.uuid4().hex[:12])
                    moved = await _cas(
                        db, key, states=[IN_FLIGHT], token=_token_pred(snap["token"]),
                        values={"state": UNKNOWN_OUTCOME, "summary": "outcome unknown: attempt went stale", "result": res},
                    )
                    if moved:
                        logger.warning("payment attempt for %s/%s went stale; held as UNKNOWN_OUTCOME", connection_id, cart_id)
                    continue  # re-read: it is UNKNOWN now (or somebody else moved it)
                return {"in_flight": True}

            # DONE and not succeeded: a decline is not a purchase, so the basket is buyable again (the recovery path).
            token, res = _claim_result({"attempt_keys": snap["attempt_keys"]}, attempt_key, retry_of_unknown=False)
            moved = await _cas(
                db, key, states=[DONE], succeeded=False,
                values={"state": IN_FLIGHT, "succeeded": None, "completed_at": None, "summary": None, "result": res},
            )
            if moved:
                return {"claimed": True, "token": token}
            # lost the race to another tap; read again to see what it did

    return {"in_flight": True}


async def retry_unknown(connection_id: str, cart_id: str, *, attempt_key: str) -> dict | None:
    """The safe same-key retry of an unresolved payment: UNKNOWN_OUTCOME -> IN_FLIGHT, or None when it is not allowed.

    Allowed only when this exact platform key was already tried for the basket (so the platform can recognise and replay the
    ORIGINAL purchase rather than create a second) and the settle window has passed since the row became unknown (so a held
    original request has had time to land). The caller has already checked the platform honours idempotency keys.
    """
    key = cart_payment_key(connection_id, cart_id)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is None or row.state != UNKNOWN_OUTCOME:
            return None
        snap = _snapshot(row)
        unknown_at = snap["unknown_at"]
        if attempt_key not in snap["attempt_keys"] or unknown_at is None:
            return None
        if (_now() - unknown_at).total_seconds() < settle_window():
            return None
        token, res = _claim_result(dict(row.result or {}), attempt_key, retry_of_unknown=True)
        full = dict(row.result or {})
        full.update(res)
        moved = await _cas(
            db, key, states=[UNKNOWN_OUTCOME], token=_token_pred(snap["token"]),
            values={"state": IN_FLIGHT, "summary": None, "result": full},
        )
        return {"claimed": True, "token": token} if moved else None


async def mark_unknown(connection_id: str, cart_id: str, *, token: str | None, reason: str) -> bool:
    """IN_FLIGHT -> UNKNOWN_OUTCOME for the attempt that holds `token`. Never releases, never overwrites a settled row."""
    key = cart_payment_key(connection_id, cart_id)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is None or row.state != IN_FLIGHT:
            return False
        res = dict(row.result or {})
        res.update({"reason": reason, "unknown_at": _iso(), "manual": False})
        res.setdefault("first_unknown_at", res["unknown_at"])
        res.setdefault("episode", uuid.uuid4().hex[:12])
        return await _cas(
            db, key, states=[IN_FLIGHT], token=_token_pred(token),
            values={"state": UNKNOWN_OUTCOME, "summary": "outcome unknown", "result": res},
        )


async def release_payment(connection_id: str, cart_id: str, *, token: str | None = None) -> bool:
    """Give the basket back after an error that PROVES nothing happened (the caller decides that - see payment_guard).

    Only the attempt that holds `token` can release, and only while it is still IN_FLIGHT. An unresolved row is never released here.
    """
    key = cart_payment_key(connection_id, cart_id)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is None or row.state != IN_FLIGHT:
            return False
        if token and (row.result or {}).get("attempt_token") != token:
            return False
        await db.delete(row)
        return True


async def _write_lines(db, key, connection_id, cart_id, order_id, currency, lines):
    for i, line in enumerate(lines or []):
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
        order_line.unit_price = str(line["unit_price"]) if line.get("unit_price") is not None else None
        order_line.line_total = str(line["line_total"]) if line.get("line_total") is not None else None
        order_line.currency = line.get("currency") or currency


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
    token: str | None = None,
    from_unknown: bool = False,
    resolution: dict | None = None,
) -> bool:
    """Record how the charge ended. True means this call settled the row; False means it had already moved and nothing changed.

    A success locks the basket for good. A decline leaves it buyable, because the shopper is about to be offered another way
    to pay and refusing them then would turn a recoverable sale into a lost one.

    Bound to a claim by `token`: the handler that holds the claim may settle it from IN_FLIGHT, or from UNKNOWN_OUTCOME when its
    own deadline already parked the row there. `from_unknown=True` is the reconciler / operator resolving an UNKNOWN row (it
    passes the token it read, so a row that moved since is not overwritten). A settled row is never overwritten.

    `lines` - one dict per product on the order, written as `OrderLine` rows only for a *succeeded*, priced order.
    """
    key = cart_payment_key(connection_id, cart_id)

    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is None or row.state == DONE:
            return False
        if row.state == UNKNOWN_OUTCOME and not (from_unknown or token):
            return False
        current = dict(row.result or {})
        if token and current.get("attempt_token") and current.get("attempt_token") != token:
            return False
        res: dict = {k: v for k, v in current.items() if k in ("attempt_keys", "attempt_token", "first_unknown_at", "unknown_at", "reason", "recon")}
        if order_id:
            res["order_id"] = order_id
        # Only a successful, priced order contributes a figure - a decline settles the row (so the ledger sees the attempt)
        # but must never be counted as sales, and this is the one place `total_sales` reads its numbers from.
        if succeeded and amount is not None:
            res["amount_paid"] = amount
            if currency:
                res["currency"] = currency
        if resolution:
            res["operator"] = resolution
        moved = await _cas(
            db, key, states=[IN_FLIGHT, UNKNOWN_OUTCOME], token=_token_pred(current.get("attempt_token")),
            values={"state": DONE, "succeeded": succeeded, "summary": summary[:500], "result": res, "completed_at": _now()},
        )
        if not moved:
            return False
        if succeeded and order_id and lines:
            await _write_lines(db, key, connection_id, cart_id, order_id, currency, lines)
        return True


async def record_reconciliation(
    connection_id: str, cart_id: str, *, status: str, manual: bool, token: str | None
) -> None:
    """Remember the last evidence check on an UNKNOWN row (status is a fixed code, never platform text)."""
    key = cart_payment_key(connection_id, cart_id)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is None or row.state != UNKNOWN_OUTCOME:
            return
        res = dict(row.result or {})
        prev = dict(res.get("recon") or {})
        res["recon"] = {"at": _iso(), "status": status, "checks": int(prev.get("checks") or 0) + 1}
        res["manual"] = bool(manual)
        await _cas(db, key, states=[UNKNOWN_OUTCOME], token=_token_pred(token), values={"result": res})


async def record_alert(
    connection_id: str, cart_id: str, *, episode: str, state: str, token: str | None
) -> None:
    """Remember that the merchant alert for this unknown-outcome EPISODE was sent (or failed, with a try count).

    An episode is one run of UNKNOWN_OUTCOME for a basket (it starts at `first_unknown_at` and survives same-key retries), so
    one basket raises one alert per episode however many taps, retries, sweeps or restarts touch it.
    """
    key = cart_payment_key(connection_id, cart_id)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is None or row.state != UNKNOWN_OUTCOME:
            return
        res = dict(row.result or {})
        prev = dict(res.get("alert") or {})
        if state == "failed" and prev.get("episode") == episode and prev.get("state") == "sent":
            return  # a racing writer already recorded success; a later failure must not downgrade it
        failed = 1 if state == "failed" else 0
        tries = (int(prev.get("tries") or 0) + failed) if prev.get("episode") == episode else failed
        res["alert"] = {"episode": episode, "state": state, "at": _iso(), "tries": tries}
        await _cas(db, key, states=[UNKNOWN_OUTCOME], token=_token_pred(token), values={"result": res})


async def stale_in_flight_to_unknown(min_age: float | None = None) -> int:
    """Sweep: IN_FLIGHT payment rows older than `min_age` (default `stale_after()`) become UNKNOWN_OUTCOME, never released.

    At engine startup the caller passes 0: there is exactly one engine process, so every IN_FLIGHT row found then belongs to a
    handler that died with the previous process. Returns how many moved.
    """
    cutoff = _now() - timedelta(seconds=stale_after() if min_age is None else min_age)
    moved = 0
    async with session_scope() as db:
        rows = await db.execute(
            select(ExecutionAttempt).where(
                ExecutionAttempt.state == IN_FLIGHT, ExecutionAttempt.action_type == "CHECKOUT"
            )
        )
        for row in list(rows.scalars()):
            snap = _snapshot(row)
            started = snap["started_at"]
            if started is None or started > cutoff:
                continue
            res = dict(row.result or {})
            res.update({"reason": "stale_in_flight", "unknown_at": _iso(), "manual": False})
            res.setdefault("first_unknown_at", res["unknown_at"])
            res.setdefault("episode", uuid.uuid4().hex[:12])
            if await _cas(
                db, row.idempotency_key, states=[IN_FLIGHT], token=_token_pred(snap["token"]),
                values={"state": UNKNOWN_OUTCOME, "summary": "outcome unknown: attempt went stale", "result": res},
            ):
                moved += 1
    return moved


async def unresolved_payments(*, limit: int = 50, offset: int = 0) -> tuple[int, list[dict]]:
    """UNKNOWN_OUTCOME payment rows, oldest first, one bounded page, plus the total. No shopper identity."""
    from sqlalchemy import func

    where = (ExecutionAttempt.state == UNKNOWN_OUTCOME, ExecutionAttempt.action_type == "CHECKOUT")
    async with session_scope() as db:
        total = (await db.execute(select(func.count()).select_from(ExecutionAttempt).where(*where))).scalar_one()
        rows = await db.execute(
            select(ExecutionAttempt).where(*where).order_by(ExecutionAttempt.created_at).offset(offset).limit(limit)
        )
        return int(total), [_snapshot(r) for r in rows.scalars()]


async def forget_payment(connection_id: str, cart_id: str) -> None:
    """Discard any record of this cart having been paid for, at the one moment the answer is not a guess: create_cart.

    Cart ids are sequential and the platform's counter lives in its own memory, so a restart reissues BSK00001 to a shopper who
    has never seen it - without this a brand new empty basket handed the same id a paid basket once had is told it is already
    bought. Also clears any `OrderLine` rows a previous life of this cart id left behind (their row ids derive from the same key).

    An IN_FLIGHT or UNKNOWN_OUTCOME row is NOT discarded: it is the only record that the basket may have been charged and
    nothing here proves otherwise. It is logged instead; a recycled id with a stale unresolved row is cleared by reconciliation
    (the platform's lookup returns nothing once its memory is gone) or by an operator.
    """
    key = cart_payment_key(connection_id, cart_id)

    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        if row is not None:
            if row.state in (IN_FLIGHT, UNKNOWN_OUTCOME):
                logger.warning("create_cart reissued %s/%s while its payment is unresolved; keeping the row", connection_id, cart_id)
                return
            await db.delete(row)

        stale_lines = await db.execute(select(OrderLine).where(OrderLine.row_id.like(f"{key}:%")))
        for line in stale_lines.scalars():
            await db.delete(line)
