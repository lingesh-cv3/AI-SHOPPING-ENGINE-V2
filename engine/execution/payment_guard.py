"""The one place a basket is charged: claim, call, classify, settle.

`/api/chat/pay`, the REST checkout and a payment recovery all used to carry their own copy of "claim the ledger row, call the
platform, release the row on any error". That last step was the defect: a TIMEOUT or a 5xx does not prove the platform did
nothing, yet the row was deleted and the shopper told "Nothing has been charged" - so a different card (a different platform
idempotency key) bought the same basket again (payment_repro/FINDINGS.md). The rules now live here, once:

- claim the basket's ledger row first (compare-and-set); an unresolved or in-flight basket refuses the second attempt;
- run the platform call under a hard deadline, with the transport's "an attempt may have been processed" signal listening;
- release the row ONLY for an error that proves nothing happened, on a call none of whose attempts was ambiguous;
- everything else - timeout, 5xx, any unexpected exception, a cancelled handler, a failed ledger write after the platform
  answered - parks the row as UNKNOWN_OUTCOME, shielded from cancellation, and never releases it;
- an UNKNOWN basket is reconciled with evidence before any new attempt, and is retried with the SAME platform key only when the
  platform is known to honour keys and the settle window has passed.

Nothing here adds an action type or lets the model near money (invariants 2 and 3): the only callers are the shopper's own pay
tap and an already-approved recovery.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from engine.db import idempotency as ledger
from engine.execution import payment_reconcile
from shared.models import CheckoutResult, CommerceError, ErrorCode
from shared.models.errors import WRITE_AMBIGUITY, WriteAmbiguity

logger = logging.getLogger(__name__)

#: Errors that PROVE the platform did not process a payment: the request was refused before any money logic could run. Not
#: RATE_LIMITED (a gateway can answer 429 after an upstream write), not stock errors (a platform can raise them after
#: authorising), nothing the transport may have retried through an ambiguous attempt.
DEFINITIVE_CODES = frozenset(
    {
        ErrorCode.CART_INVALID,
        ErrorCode.CART_NOT_FOUND,
        ErrorCode.VALIDATION_ERROR,
        ErrorCode.CAPABILITY_UNSUPPORTED,
        ErrorCode.AUTH_FAILED,
    }
)

#: Shown to shoppers. Fixed strings - platform text never reaches a shopper (invariant 5).
IN_FLIGHT_REPLY = "That is going through now - give it a moment rather than paying again."
UNKNOWN_REPLY = (
    "We couldn't confirm whether your payment went through, so your basket is on hold to avoid charging you twice. "
    "Please don't pay again - if you were charged, the shop can confirm it from their records."
)
NOT_PROCESSED_REPLY = (
    "Something went wrong taking the payment, and it was not your card. Nothing has been charged - try again in a moment."
)
EMPTY_BASKET_REPLY = "There's nothing in this basket to pay for yet."
CARD_DECLINED_REPLY = "That card was declined, so nothing was charged. You can try another card."

_BACKGROUND: set[asyncio.Task] = set()


@dataclass
class PayOutcome:
    """How one payment request ended. `kind` is one of: paid, declined, already_paid, in_flight, unknown, not_processed."""

    kind: str
    result: CheckoutResult | None = None
    order_id: str | None = None
    error_code: str | None = None
    reply: str = ""
    detail: dict = field(default_factory=dict)


def platform_key(cart_id: str, card_last4: str) -> str:
    """The idempotency key handed to the platform: the basket and the card. (The ENGINE's guard is keyed on the basket alone.)"""
    return f"pay-{cart_id}-{card_last4}"


async def _shielded(coro) -> object:
    """Run a ledger write to completion even if the awaiting handler is cancelled."""
    task = asyncio.ensure_future(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return await asyncio.shield(task)


shielded = _shielded  # public name: other money paths (recovery) settle through the same cancellation-proof write


async def _park_unknown(connection_id: str, cart_id: str, token: str | None, reason: str) -> None:
    try:
        await _shielded(ledger.mark_unknown(connection_id, cart_id, token=token, reason=reason))
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        logger.exception("could not park a payment as UNKNOWN_OUTCOME")


def _unknown(reason: str, **detail) -> PayOutcome:
    return PayOutcome(kind="unknown", error_code=reason, reply=UNKNOWN_REPLY, detail=detail)


async def claim(adapter, connection_id: str, cart_id: str, key: str) -> tuple[dict | None, PayOutcome | None]:
    """Take the basket's ledger row for one attempt. Returns (claim, None) to proceed or (None, outcome) when refused."""
    begun = await ledger.begin_payment(connection_id, cart_id, attempt_key=key)

    if begun.get("unknown"):
        # Look for evidence before refusing: the platform may already show the order (or conclusively none).
        await payment_reconcile.reconcile(adapter, connection_id, cart_id)
        begun = await ledger.begin_payment(connection_id, cart_id, attempt_key=key)

    if begun.get("unknown"):
        retried = None
        if getattr(adapter, "honours_idempotency_keys", False):
            retried = await ledger.retry_unknown(connection_id, cart_id, attempt_key=key)
        if retried is None:
            return None, _unknown(begun["row"].get("reason") or "unknown_outcome", blocked=True)
        return {"token": retried["token"], "retry_of_unknown": True}, None

    if begun.get("already_paid"):
        return None, PayOutcome(kind="already_paid", order_id=begun.get("order_id"), detail={"summary": begun.get("summary")})
    if begun.get("in_flight"):
        return None, PayOutcome(kind="in_flight", reply=IN_FLIGHT_REPLY)
    return {"token": begun["token"], "retry_of_unknown": False}, None


async def guarded_call(
    connection_id: str, cart_id: str, token: str | None, call: Callable[[], Awaitable], *, retry_of_unknown: bool = False
) -> tuple[object | None, PayOutcome | None]:
    """Run one platform money call for a claimed basket. Returns (value, None) on an answer, (None, outcome) on any failure.

    By the time this returns a failure the ledger already says the right thing: released for a proven non-event, settled as
    declined for a decline raised as an error, UNKNOWN_OUTCOME for everything else.
    """
    holder = WriteAmbiguity()
    reset = WRITE_AMBIGUITY.set(holder)
    try:
        try:
            async with asyncio.timeout(ledger.call_deadline()):
                return await call(), None
        except asyncio.CancelledError:
            # The handler was cancelled (client gone, shutdown): the platform may still have acted.
            await _park_unknown(connection_id, cart_id, token, "handler_cancelled")
            raise
        except TimeoutError:
            await _park_unknown(connection_id, cart_id, token, "handler_timeout")
            return None, _unknown("handler_timeout")
        except CommerceError as exc:
            code = exc.code
            if code == ErrorCode.PAYMENT_DECLINED:
                await ledger.payment_settled(connection_id, cart_id, succeeded=False, order_id=None, summary="declined", token=token)
                return None, PayOutcome(kind="not_processed", error_code=str(code), reply=CARD_DECLINED_REPLY, detail={"declined": True})
            if code in DEFINITIVE_CODES and not holder.ambiguous and not retry_of_unknown:
                await ledger.release_payment(connection_id, cart_id, token=token)
                reply = EMPTY_BASKET_REPLY if code == ErrorCode.CART_INVALID else NOT_PROCESSED_REPLY
                return None, PayOutcome(kind="not_processed", error_code=str(code), reply=reply)
            reason = "retry_unresolved" if retry_of_unknown else ("ambiguous_prior_attempt" if code in DEFINITIVE_CODES else str(code))
            await _park_unknown(connection_id, cart_id, token, reason)
            return None, _unknown(reason)
        except Exception:  # noqa: BLE001 - anything unexpected: the platform may have acted
            logger.exception("unexpected failure during a payment call")
            await _park_unknown(connection_id, cart_id, token, "unexpected_exception")
            return None, _unknown("unexpected_exception")
    finally:
        WRITE_AMBIGUITY.reset(reset)


async def pay_basket(adapter, connection_id: str, cart_id: str, card_last4: str) -> PayOutcome:
    """The shopper's own pay tap, from either route."""
    key = platform_key(cart_id, card_last4)
    claimed, refused = await claim(adapter, connection_id, cart_id, key)
    if refused is not None:
        return refused
    token = claimed["token"]

    result, failure = await guarded_call(
        connection_id, cart_id, token,
        lambda: adapter.checkout_with_card(cart_id, card_last4=card_last4, idempotency_key=key),
        retry_of_unknown=claimed["retry_of_unknown"],
    )
    if failure is not None:
        return failure

    order = result.order
    args = payment_reconcile.order_settlement_args(order, succeeded=result.succeeded)
    try:
        settled = await _shielded(
            ledger.payment_settled(
                connection_id, cart_id, succeeded=result.succeeded, token=token,
                summary=(f"paid {args['order_id']}" if result.succeeded else f"declined on {args['order_id']}"), **args,
            )
        )
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 - the platform answered but the ledger could not record it
        logger.exception("could not record a payment result")
        await _park_unknown(connection_id, cart_id, token, "post_charge_write_failed")
        return _unknown("post_charge_write_failed")
    if not settled:
        logger.error("payment result for %s/%s could not be recorded: the ledger row had already moved", connection_id, cart_id)
        return _unknown("ledger_moved")

    return PayOutcome(kind="paid" if result.succeeded else "declined", result=result, order_id=args["order_id"])
