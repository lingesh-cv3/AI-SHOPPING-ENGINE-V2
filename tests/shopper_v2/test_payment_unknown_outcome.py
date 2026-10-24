"""Payment safety: an ambiguous payment outcome is held, never released, and never charged twice (model-free).

Regression for payment_repro/FINDINGS.md: after a TIMEOUT or 5xx the engine deleted the basket's ledger row and told the shopper
"Nothing has been charged", so a different card (a different platform idempotency key) bought the same basket again. These tests
drive the real ledger and the real guard (`engine/execution/payment_guard.py`) against a scripted fake platform that really
records purchases, and assert on how many purchases the PLATFORM holds for one basket.

    python tests/shopper_v2/test_payment_unknown_outcome.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_pay_unknown.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_pay_unknown.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402
import json  # noqa: E402

from _pay_fixtures import FakePlatform, falsely_reassures, says_unknown  # noqa: E402
from shared.models import ErrorCode  # noqa: E402

failures = 0
CONN = "conn_t"
SECRET = "RAW-PLATFORM-SECRET-TEXT"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def row_of(cart):
    from engine.db import idempotency as ledger

    return await ledger.payment_row(CONN, cart)


async def pay(platform, cart, card="1111"):
    from engine.execution import payment_guard

    return await payment_guard.pay_basket(platform, CONN, cart, card)


async def age(cart, *, started=None, unknown=None):
    """Move a row's clocks into the past (what the passage of time would do)."""
    from datetime import UTC, datetime, timedelta

    from engine.db import idempotency as ledger
    from engine.db.models import ExecutionAttempt
    from engine.db.session import session_scope

    key = ledger.cart_payment_key(CONN, cart)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        res = dict(row.result or {})
        if started is not None:
            res["started_at"] = (datetime.now(UTC) - timedelta(seconds=started)).isoformat()
        if unknown is not None:
            res["unknown_at"] = (datetime.now(UTC) - timedelta(seconds=unknown)).isoformat()
        row.result = res


async def checks():
    from engine.db import idempotency as ledger

    os.environ["PAYMENT_SETTLE_WINDOW_SECONDS"] = "3600"  # nothing is conclusive unless a test says so

    print("1. Timeout after the platform charged, then a DIFFERENT card (repro S2)")
    p = FakePlatform(["charge_then_timeout"])
    a = await pay(p, "C-S2", "1111")
    check("the first tap ends UNKNOWN, with the fixed 'couldn't confirm' wording", a.kind == "unknown" and says_unknown(a.reply), (a.kind, a.reply))
    check("it does NOT say nothing was charged", not falsely_reassures(a.reply), a.reply)
    r = await row_of("C-S2")
    check("the ledger row is UNKNOWN_OUTCOME with the reason code, keeping the attempted key", r["state"] == "UNKNOWN_OUTCOME" and r["reason"] == "TIMEOUT" and r["attempt_keys"] == ["pay-C-S2-1111"], r)
    b = await pay(p, "C-S2", "2222")
    check("the different card is refused without calling the platform", b.kind == "unknown" and p.checkout_calls == ["pay-C-S2-1111"], (b.kind, p.checkout_calls))
    check("the platform holds exactly ONE purchase for the basket", len(p.purchases_for("C-S2")) == 1, p.purchases)

    print("\n2. A 5xx after the platform charged (repro S3), then a different card")
    p = FakePlatform(["charge_then_5xx"])
    a = await pay(p, "C-S3", "1111")
    b = await pay(p, "C-S3", "2222")
    check("held as UNKNOWN and the second card never reaches the platform", a.kind == "unknown" and b.kind == "unknown" and len(p.checkout_calls) == 1 and len(p.purchases_for("C-S3")) == 1, (a.kind, b.kind, p.checkout_calls))
    check("no platform text in the replies or the ledger row", SECRET not in a.reply + b.reply and SECRET not in json.dumps(await row_of("C-S3"), default=str))

    print("\n3. A conclusive error that followed an AMBIGUOUS earlier attempt is not conclusive")
    p = FakePlatform([("ambiguous_then_error", ErrorCode.CART_INVALID)])
    a = await pay(p, "C-AMB", "1111")
    r = await row_of("C-AMB")
    check("the basket is held (an earlier attempt of the same call may have been processed), not released",
          a.kind == "unknown" and r is not None and r["state"] == "UNKNOWN_OUTCOME" and r["reason"] == "ambiguous_prior_attempt", (a.kind, r))

    print("\n4. A proven non-event releases the basket (no regression of recovery)")
    p = FakePlatform([("error", ErrorCode.CART_INVALID), "ok"])
    a = await pay(p, "C-EMPTY", "1111")
    check("CART_INVALID with a clean call: nothing was charged, said so, row gone", a.kind == "not_processed" and "nothing in this basket" in a.reply.lower() and await row_of("C-EMPTY") is None, (a.kind, a.reply))
    b = await pay(p, "C-EMPTY", "2222")
    check("the basket is immediately payable again", b.kind == "paid", b.kind)
    for code in (ErrorCode.RATE_LIMITED, ErrorCode.INVENTORY_INSUFFICIENT, ErrorCode.CHECKOUT_FAILED, ErrorCode.UPSTREAM_ERROR):
        q = FakePlatform([("error", code)])
        o = await pay(q, f"C-NOT-{code}", "1111")
        check(f"{code} does NOT prove nothing happened: held, not released", o.kind == "unknown" and (await row_of(f"C-NOT-{code}"))["state"] == "UNKNOWN_OUTCOME", o.kind)

    print("\n5. A decline is a settled non-charge: another card may pay")
    p = FakePlatform(["decline", "ok"])
    a = await pay(p, "C-DEC", "0002")
    check("the decline settles as declined", a.kind == "declined" and (await row_of("C-DEC"))["state"] == "DONE", a.kind)
    b = await pay(p, "C-DEC", "1111")
    check("the second card then pays, one paid purchase", b.kind == "paid" and len(p.purchases_for("C-DEC")) == 1, (b.kind, p.purchases))
    c = await pay(p, "C-DEC", "3333")
    check("and a paid basket cannot be charged again", c.kind == "already_paid" and len(p.checkout_calls) == 2, (c.kind, p.checkout_calls))
    q = FakePlatform([("error", ErrorCode.PAYMENT_DECLINED)])
    d = await pay(q, "C-DECERR", "0002")
    check("a decline raised as an error settles as declined (not released, not unknown)", d.kind == "not_processed" and (await row_of("C-DECERR"))["state"] == "DONE", (d.kind, await row_of("C-DECERR")))
    check("and the shopper is told the card was declined, not that something went wrong with the platform", "declined" in d.reply.lower() and "something went wrong" not in d.reply.lower(), d.reply)

    print("\n6. Unexpected exceptions are held, never left IN_FLIGHT")
    p = FakePlatform(["unexpected"])
    a = await pay(p, "C-EXC", "1111")
    r = await row_of("C-EXC")
    check("an unexpected exception parks UNKNOWN_OUTCOME and the tap still gets an honest reply", a.kind == "unknown" and r["state"] == "UNKNOWN_OUTCOME" and r["reason"] == "unexpected_exception", (a.kind, r))

    print("\n7. A platform call that never returns is cut off by the deadline")
    os.environ["PAYMENT_CALL_DEADLINE_SECONDS"] = "0.4"
    p = FakePlatform(["hang"])
    a = await pay(p, "C-HANG", "1111")
    check("the deadline parks UNKNOWN_OUTCOME (handler_timeout)", a.kind == "unknown" and (await row_of("C-HANG"))["reason"] == "handler_timeout", (a.kind, await row_of("C-HANG")))
    del os.environ["PAYMENT_CALL_DEADLINE_SECONDS"]

    print("\n8. The client disconnects mid-payment (handler cancelled) - repro S4")
    p = FakePlatform(["hang", "ok"])
    task = asyncio.create_task(pay(p, "C-CANCEL", "1111"))
    await asyncio.sleep(0.3)
    task.cancel()
    try:
        await task
        cancelled = False
    except asyncio.CancelledError:
        cancelled = True
    await asyncio.sleep(0.1)
    r = await row_of("C-CANCEL")
    check("the cancelled handler leaves UNKNOWN_OUTCOME (handler_cancelled), not IN_FLIGHT", cancelled and r["state"] == "UNKNOWN_OUTCOME" and r["reason"] == "handler_cancelled", (cancelled, r))
    b = await pay(p, "C-CANCEL", "2222")
    check("a different card afterwards is refused, platform asked once", b.kind == "unknown" and len(p.checkout_calls) == 1, (b.kind, p.checkout_calls))

    print("\n9. The platform answered but the ledger write failed")
    p = FakePlatform(["ok"])
    real = ledger.payment_settled

    async def boom(*a, **k):
        raise RuntimeError("disk full")

    ledger.payment_settled = boom
    try:
        a = await pay(p, "C-WRITE", "1111")
    finally:
        ledger.payment_settled = real
    r = await row_of("C-WRITE")
    check("held as UNKNOWN_OUTCOME (post_charge_write_failed): the charge exists, the record does not", a.kind == "unknown" and r["reason"] == "post_charge_write_failed" and len(p.purchases_for("C-WRITE")) == 1, (a.kind, r))

    print("\n10. Concurrent taps")
    p = FakePlatform(["slow_ok", "ok"])
    x, y = await asyncio.gather(pay(p, "C-CONC", "1111"), pay(p, "C-CONC", "2222"))
    kinds = sorted([x.kind, y.kind])
    check("two cards at once: one pays, the other is told it is in flight, the platform is asked once", kinds == ["in_flight", "paid"] and len(p.checkout_calls) == 1, (kinds, p.checkout_calls))
    p = FakePlatform(["slow_ok", "ok"])
    x, y = await asyncio.gather(pay(p, "C-CONC2", "1111"), pay(p, "C-CONC2", "1111"))
    check("the same card twice at once: still one platform call, one purchase", len(p.checkout_calls) == 1 and len(p.purchases_for("C-CONC2")) == 1, (p.checkout_calls, p.purchases))

    print("\n11. Same-key retry of an unresolved payment")
    os.environ["PAYMENT_SETTLE_WINDOW_SECONDS"] = "3600"
    p = FakePlatform(["charge_then_timeout"])
    await pay(p, "C-SAME", "1111")
    early = await pay(p, "C-SAME", "1111")
    check("inside the settle window the same card is refused too (a held request may still land)", early.kind == "unknown" and len(p.checkout_calls) == 1, (early.kind, p.checkout_calls))
    await age("C-SAME", unknown=7200)
    late = await pay(p, "C-SAME", "1111")
    check("after the window the SAME key is retried and settles with the ORIGINAL order id, one purchase",
          late.kind == "paid" and late.order_id == p.purchases[0]["ref"] and len(p.purchases_for("C-SAME")) == 1, (late.kind, late.order_id, p.purchases))
    q = FakePlatform(["charge_then_timeout"], honours=False)
    await pay(q, "C-NOHON", "1111")
    await age("C-NOHON", unknown=7200)
    r2 = await pay(q, "C-NOHON", "1111")
    check("a platform that does not honour idempotency keys is never retried blindly", r2.kind == "unknown" and len(q.checkout_calls) == 1, (r2.kind, q.checkout_calls))
    z = FakePlatform(["charge_then_timeout", ("error", ErrorCode.CART_INVALID)])
    await pay(z, "C-RETRYERR", "1111")
    await age("C-RETRYERR", unknown=7200)
    e = await pay(z, "C-RETRYERR", "1111")
    check("a retry that ends in a 'conclusive' error does NOT release (the earlier attempt is still unresolved)",
          e.kind == "unknown" and (await row_of("C-RETRYERR"))["state"] == "UNKNOWN_OUTCOME", (e.kind, await row_of("C-RETRYERR")))
    other = await pay(z, "C-RETRYERR", "2222")
    check("and a different card is still refused", other.kind == "unknown", other.kind)

    print("\n12. A crashed process leaves a stale IN_FLIGHT row")
    p = FakePlatform()
    begun = await ledger.begin_payment(CONN, "C-CRASH", attempt_key="pay-C-CRASH-1111")  # claimed, then the process 'died'
    check("precondition: claimed and in flight", begun.get("claimed") and (await row_of("C-CRASH"))["state"] == "IN_FLIGHT")
    mid = await pay(p, "C-CRASH", "2222")
    check("while fresh, another tap is told it is in flight", mid.kind == "in_flight" and p.checkout_calls == [], (mid.kind, p.checkout_calls))
    await age("C-CRASH", started=100000)
    late = await pay(p, "C-CRASH", "2222")
    r = await row_of("C-CRASH")
    check("once stale it becomes UNKNOWN_OUTCOME (never released) and the tap is refused without a platform call", late.kind == "unknown" and r["reason"] == "stale_in_flight" and p.checkout_calls == [], (late.kind, r, p.checkout_calls))
    await ledger.begin_payment(CONN, "C-ORPHAN", attempt_key="pay-C-ORPHAN-1111")
    moved = await ledger.stale_in_flight_to_unknown(0)
    check("the startup sweep (min_age 0) parks every orphaned IN_FLIGHT row", moved >= 1 and (await row_of("C-ORPHAN"))["state"] == "UNKNOWN_OUTCOME", (moved, await row_of("C-ORPHAN")))

    print("\n13. The ledger's own guards")
    check("payment_lock reports each state", await ledger.payment_lock(CONN, "C-S2") == "unknown" and await ledger.payment_lock(CONN, "C-DEC") == "paid"
          and await ledger.payment_lock(CONN, "C-DECERR") is None and await ledger.payment_lock(CONN, "NOPE") is None)
    await ledger.forget_payment(CONN, "C-S2")
    check("create_cart's forget_payment does NOT delete an unresolved row", (await row_of("C-S2")) is not None)
    check("purge_old never drops an unresolved row", await ledger.purge_old() >= 0 and (await row_of("C-S2")) is not None)
    stale_settle = await ledger.payment_settled(CONN, "C-S2", succeeded=True, order_id="ORDX", summary="x", token="not-the-token")
    check("a settle with the wrong claim token is refused and the row is unchanged", stale_settle is False and (await row_of("C-S2"))["state"] == "UNKNOWN_OUTCOME", stale_settle)
    done = await ledger.payment_settled(CONN, "C-DEC", succeeded=False, order_id=None, summary="late", token="whatever")
    check("a settled (paid) row is never overwritten", done is False and (await row_of("C-DEC"))["state"] == "DONE" and (await row_of("C-DEC"))["succeeded"] is True, done)

    print("\n14. Cart changes are refused while a payment is paid, in flight or unresolved")
    from fastapi import HTTPException

    from engine.api import shop

    refused = {}
    for cart in ("C-S2", "C-CRASH", "C-DEC"):
        try:
            await shop._not_if_paid(CONN, cart)
            refused[cart] = None
        except HTTPException as exc:
            refused[cart] = exc.detail["code"]
    check("unknown -> CART_PAYMENT_PENDING, paid -> CART_ALREADY_PAID", refused == {"C-S2": "CART_PAYMENT_PENDING", "C-CRASH": "CART_PAYMENT_PENDING", "C-DEC": "CART_ALREADY_PAID"}, refused)
    try:
        await shop._not_if_paid(CONN, "C-DECERR")
        free = True
    except HTTPException:
        free = False
    check("a declined basket may still be changed", free)


async def main():
    from engine.db import create_schema, dispose

    await create_schema()
    try:
        await checks()
    finally:
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
