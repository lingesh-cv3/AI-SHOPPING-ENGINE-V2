"""Evidence-based reconciliation and operator resolution of unresolved payments (model-free).

Only a confirmed order makes the basket paid; only a COMPLETE platform answer showing no charge, after the settle window, makes it
buyable again; everything inconclusive stays blocked. An operator's resolution never outranks the platform's own records.

    python tests/shopper_v2/test_payment_reconcile.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_pay_reconcile.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_pay_reconcile.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402
import json  # noqa: E402

from _pay_fixtures import FakePlatform, FakePlatformWithLookup, make_order  # noqa: E402

failures = 0
CONN = "conn_t"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def row_of(cart, conn=CONN):
    from engine.db import idempotency as ledger

    return await ledger.payment_row(conn, cart)


async def pay(platform, cart, card="1111", conn=CONN):
    from engine.execution import payment_guard

    return await payment_guard.pay_basket(platform, conn, cart, card)


async def age(cart, *, unknown=None, recon_at_clear=False, conn=CONN):
    from datetime import UTC, datetime, timedelta

    from engine.db import idempotency as ledger
    from engine.db.models import ExecutionAttempt
    from engine.db.session import session_scope

    key = ledger.cart_payment_key(conn, cart)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        res = dict(row.result or {})
        if unknown is not None:
            res["unknown_at"] = (datetime.now(UTC) - timedelta(seconds=unknown)).isoformat()
        if recon_at_clear:
            res.pop("recon", None)
        row.result = res


async def held(platform, cart, script_step="charge_then_timeout"):
    platform.script.insert(0, script_step)
    return await pay(platform, cart)


async def checks():
    from engine import db
    from engine.db import idempotency as ledger
    from engine.execution import payment_reconcile as rec

    os.environ["PAYMENT_SETTLE_WINDOW_SECONDS"] = "3600"

    print("1. The platform shows a PAID order: the basket becomes paid with the platform's order id")
    p = FakePlatformWithLookup()
    await db.owners.take(CONN, db.owners.CART, "R-PAID", "owner-key-1")
    await held(p, "R-PAID")
    out = await rec.reconcile(p, CONN, "R-PAID")
    r = await row_of("R-PAID")
    check("reconcile settles DONE (paid) with the platform's order id", out["status"] == "paid" and r["state"] == "DONE" and r["succeeded"] is True and r["order_id"] == p.purchases[0]["ref"], (out, r))
    check("the order is owned by the CART's owner (so the shopper can see it)", await db.owners.owner_of(CONN, db.owners.ORDER, p.purchases[0]["ref"]) == "owner-key-1")
    late = await pay(p, "R-PAID", "2222")
    check("the basket is now bought: a second card is told so and the platform is not asked again", late.kind == "already_paid" and len(p.checkout_calls) == 1, (late.kind, p.checkout_calls))

    print("\n2. Ownership is never created from a first touch, and never overwritten")
    p = FakePlatformWithLookup()
    p._seq = __import__("itertools").count(700)  # a ref no earlier check has given an owner
    await held(p, "R-NOOWN")
    await rec.reconcile(p, CONN, "R-NOOWN")
    check("a basket with no recorded owner leaves the order ownerless (no first-touch claim)", await db.owners.owner_of(CONN, db.owners.ORDER, p.purchases[0]["ref"]) is None)
    p = FakePlatformWithLookup()
    await db.owners.take(CONN, db.owners.CART, "R-OWNED", "owner-key-A")
    await db.owners.take(CONN, db.owners.ORDER, "ORD0001", "someone-else")
    await held(p, "R-OWNED")
    await rec.reconcile(p, CONN, "R-OWNED")
    check("an existing order owner is not replaced", await db.owners.owner_of(CONN, db.owners.ORDER, "ORD0001") == "someone-else")

    print("\n3. Absence is conclusive only when COMPLETE and after the settle window")
    p = FakePlatformWithLookup()
    p.script.append("ok")
    q = FakePlatformWithLookup()
    await held(q, "R-ABS")
    q.purchases.clear()  # the platform genuinely holds nothing (the call was held and never landed)
    early = await rec.reconcile(q, CONN, "R-ABS")
    check("absent too early: stays UNKNOWN (a held request could still land)", early["status"] == "absent_too_early" and (await row_of("R-ABS"))["state"] == "UNKNOWN_OUTCOME", (early, await row_of("R-ABS")))
    await age("R-ABS", unknown=7200, recon_at_clear=True)
    clear = await rec.reconcile(q, CONN, "R-ABS")
    r = await row_of("R-ABS")
    check("absent after the window: 'confirmed not charged' - buyable again, not paid", clear["status"] == "cleared" and r["state"] == "DONE" and r["succeeded"] is False, (clear, r))
    again = await pay(q, "R-ABS", "2222")
    check("a different card can now pay (exactly one purchase)", again.kind == "paid" and len(q.purchases_for("R-ABS")) == 1, (again.kind, q.purchases))

    print("\n4. Only-unpaid orders on the platform, after the window, also clear the basket")
    u = FakePlatformWithLookup()
    await held(u, "R-UNPAID")
    u.purchases[0]["paid"] = False  # the platform's CURRENT status: the order was declined
    await age("R-UNPAID", unknown=7200, recon_at_clear=True)
    out = await rec.reconcile(u, CONN, "R-UNPAID")
    check("declined-only orders and an old row: cleared", out["status"] == "cleared" and (await row_of("R-UNPAID"))["succeeded"] is False, out)
    v = FakePlatformWithLookup()
    await held(v, "R-UNPAID-EARLY")
    v.purchases[0]["paid"] = False
    out = await rec.reconcile(v, CONN, "R-UNPAID-EARLY")
    check("the same evidence inside the window is not enough", out["status"] == "found_unpaid_too_early" and (await row_of("R-UNPAID-EARLY"))["state"] == "UNKNOWN_OUTCOME", out)

    print("\n5. Inconclusive evidence keeps the basket blocked")
    for mode, expect in (("fails", "lookup_failed"), ("partial", "lookup_inconclusive")):
        w = FakePlatformWithLookup(lookup_mode=mode)
        await held(w, f"R-{mode}")
        await age(f"R-{mode}", unknown=7200)
        out = await rec.reconcile(w, CONN, f"R-{mode}")
        r = await row_of(f"R-{mode}")
        check(f"lookup {mode}: {expect}, still UNKNOWN_OUTCOME, flagged for a person", out["status"] == expect and r["state"] == "UNKNOWN_OUTCOME" and r["manual"] is True, (out, r))
    n = FakePlatform()  # Kettle-like: no lookup by basket at all
    await held(n, "R-NOLOOKUP")
    await age("R-NOLOOKUP", unknown=7200)
    out = await rec.reconcile(n, CONN, "R-NOLOOKUP")
    check("a platform that cannot look an order up: no_lookup, blocked, flagged for a person", out["status"] == "no_lookup" and (await row_of("R-NOLOOKUP"))["manual"] is True, out)
    t = FakePlatformWithLookup()
    await held(t, "R-THROTTLE")
    t.purchases.clear()
    await rec.reconcile(t, CONN, "R-THROTTLE")
    calls = t.lookup_calls
    out = await rec.reconcile(t, CONN, "R-THROTTLE")
    check("a second check straight after is throttled (no extra platform lookup)", out["status"] == "throttled" and t.lookup_calls == calls, (out, t.lookup_calls, calls))
    out = await rec.reconcile(t, CONN, "R-THROTTLE", force=True)
    check("an operator's explicit reconcile bypasses the throttle", out["status"] != "throttled" and t.lookup_calls == calls + 1, (out, t.lookup_calls))

    print("\n6. A blocked tap reconciles first")
    p = FakePlatformWithLookup()
    await held(p, "R-TAP")
    nxt = await pay(p, "R-TAP", "2222")
    check("the next tap finds the paid order via the lookup and says the basket is already paid (no second charge)", nxt.kind == "already_paid" and len(p.purchases_for("R-TAP")) == 1 and len(p.checkout_calls) == 1, (nxt.kind, p.checkout_calls))

    print("\n7. The platform double-charged before we looked: settle once, loudly")
    d = FakePlatformWithLookup()
    await held(d, "R-DUP")
    d.purchases.append({"ref": "ORD-DUP2", "cart": "R-DUP", "key": "pay-R-DUP-2222", "paid": True})
    out = await rec.reconcile(d, CONN, "R-DUP", force=True)
    check("settled to a paid order, duplicate counted for a person", out["status"] == "paid" and out["duplicates"] == 1 and (await row_of("R-DUP"))["state"] == "DONE", out)

    print("\n8. Operator resolution never outranks the platform")
    p = FakePlatformWithLookup()
    await held(p, "O-1")
    refusal = await rec.resolve(p, CONN, "O-1", resolution="not_charged", order_id=None, note="customer says no")
    check("not_charged is REFUSED while the platform shows a paid order", refusal["status"] == "refused_paid_on_platform" and (await row_of("O-1"))["state"] == "UNKNOWN_OUTCOME", refusal)
    q = FakePlatformWithLookup()
    await held(q, "O-2")
    q.purchases.clear()
    refusal = await rec.resolve(q, CONN, "O-2", resolution="paid", order_id="ORD-FAKE", note="")
    check("paid is REFUSED when the platform answers completely and holds nothing", refusal["status"] == "refused_absent_on_platform" and (await row_of("O-2"))["state"] == "UNKNOWN_OUTCOME", refusal)
    ok = await rec.resolve(p, CONN, "O-1", resolution="paid", order_id=None, note="confirmed")
    r = await row_of("O-1")
    check("paid with no order id adopts the platform's single paid order; audited as the operator, not asserted", ok["status"] == "resolved" and r["state"] == "DONE" and r["operator"].get("by") == "operator" and r["operator"].get("asserted") is False, (ok, r))
    wrong = FakePlatformWithLookup()
    await held(wrong, "O-3")
    refusal = await rec.resolve(wrong, CONN, "O-3", resolution="paid", order_id="ORD-NOT-THERE", note="")
    check("paid with an order id the platform does not show is refused", refusal["status"] == "refused_order_not_found_on_platform", refusal)
    k = FakePlatform()  # no lookup: the operator's call stands, recorded as asserted
    await held(k, "O-4")
    need = await rec.resolve(k, CONN, "O-4", resolution="paid", order_id=None, note="")
    check("without a lookup, 'paid' needs an order id", need["status"] == "order_id_required", need)
    asserted = await rec.resolve(k, CONN, "O-4", resolution="paid", order_id="KB-0042", note="seen in the back office")
    r = await row_of("O-4")
    check("with an order id and no lookup it stands, recorded operator-asserted", asserted["status"] == "resolved" and r["operator"].get("asserted") is True and r["order_id"] == "KB-0042", (asserted, r))
    k2 = FakePlatform()
    await held(k2, "O-5")
    early = await rec.resolve(k2, CONN, "O-5", resolution="not_charged", order_id=None, note="too soon")
    check("not_charged is refused inside the settle window (a held request may still land)", early["status"] == "refused_too_early" and (await row_of("O-5"))["state"] == "UNKNOWN_OUTCOME", early)
    await age("O-5", unknown=7200)
    nc = await rec.resolve(k2, CONN, "O-5", resolution="not_charged", order_id=None, note="refunded by the bank", operator_id="key_op_7")
    r5 = await row_of("O-5")
    check("after the window, without a lookup, 'not_charged' stands, the basket is buyable again, and the audit names the operator key", nc["status"] == "resolved" and r5["succeeded"] is False and r5["operator"].get("by") == "key_op_7", (nc, r5))
    gone = await rec.resolve(k2, CONN, "O-5", resolution="paid", order_id="X", note="")
    check("a basket that is no longer unresolved cannot be resolved again", gone["status"] == "not_found", gone)
    check("the audit trail holds fixed codes and the operator's note only (no platform text)", "RAW-PLATFORM-SECRET-TEXT" not in json.dumps(await row_of("O-1"), default=str))

    print("\n9. Recovery shares the basket's claim")
    from engine.execution import payment_guard

    rp = FakePlatformWithLookup()
    await held(rp, "RV-UNKNOWN")
    claimed, refused = await payment_guard.claim(rp, CONN, "RV-UNKNOWN", "recovery-key-1")
    check("a recovery on an UNKNOWN basket is refused (found paid -> already paid)", claimed is None and refused is not None and refused.kind == "already_paid", refused and refused.kind)
    rn = FakePlatform()
    await held(rn, "RV-UNKNOWN2")
    claimed, refused = await payment_guard.claim(rn, CONN, "RV-UNKNOWN2", "recovery-key-2")
    check("with no way to check, a recovery on an UNKNOWN basket is refused as unknown", claimed is None and refused.kind == "unknown", refused and refused.kind)
    rd = FakePlatform(["decline"])
    await pay(rd, "RV-DECL", "0002")
    claimed, refused = await payment_guard.claim(rd, CONN, "RV-DECL", "recovery-key-3")
    check("a declined basket (the recovery case) can be claimed", claimed is not None and (await row_of("RV-DECL"))["state"] == "IN_FLIGHT", (claimed, refused))
    again, refused2 = await payment_guard.claim(rd, CONN, "RV-DECL", "recovery-key-4")
    check("while the recovery is in flight, a pay tap or a second recovery cannot also claim it", again is None and refused2.kind == "in_flight", refused2 and refused2.kind)
    from engine.shopper_v2 import state as _unused  # noqa: F401  (the import proves the package is loadable alongside)

    print("\n10. The sweep")

    class Registry:
        def __init__(self, platform):
            self.platform = platform

        def adapter_for(self, connection_id):
            return self.platform if connection_id == CONN else None

    sp = FakePlatformWithLookup()
    await held(sp, "SW-1")
    await ledger.begin_payment(CONN, "SW-STALE", attempt_key="pay-SW-STALE-1111")
    await age("SW-STALE")  # fresh clocks: not stale yet
    out = await rec.sweep(Registry(sp))
    check("the sweep reconciles an unresolved basket (found paid -> settled)", (await row_of("SW-1"))["state"] == "DONE" and out["paid"] >= 1, out)


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
