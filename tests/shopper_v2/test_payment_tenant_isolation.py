"""Tenant isolation of the payment ledger, reconciliation and operator resolution (model-free).

The same cart id under two merchants is two different baskets. One merchant's unresolved payment never blocks, settles or leaks
into another's; reconciliation asks only the adapter of the row's own connection; an operator resolving under the wrong
connection finds nothing.

    python tests/shopper_v2/test_payment_tenant_isolation.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_pay_tenant.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_pay_tenant.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402

from _pay_fixtures import FakePlatformWithLookup  # noqa: E402

failures = 0
A, B = "conn_a", "conn_b"
CART = "SAME-CART-ID"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks():
    from engine import db
    from engine.db import idempotency as ledger
    from engine.execution import payment_guard, payment_reconcile as rec

    os.environ["PAYMENT_SETTLE_WINDOW_SECONDS"] = "3600"

    print("1. The same cart id under two merchants is two baskets")
    pa = FakePlatformWithLookup(["charge_then_timeout"])
    pb = FakePlatformWithLookup()
    a = await payment_guard.pay_basket(pa, A, CART, "1111")
    b = await payment_guard.pay_basket(pb, B, CART, "1111")
    check("A is UNKNOWN, but B's payment of the same cart id goes through", a.kind == "unknown" and b.kind == "paid", (a.kind, b.kind))
    check("each ledger row is its own (different state per connection)", (await ledger.payment_row(A, CART))["state"] == "UNKNOWN_OUTCOME" and (await ledger.payment_row(B, CART))["state"] == "DONE")
    check("A's platform was asked once, B's once", len(pa.checkout_calls) == 1 and len(pb.checkout_calls) == 1)

    print("\n2. Reconciliation asks only the row's own adapter")
    await db.owners.take(A, db.owners.CART, CART, "owner-a")
    await db.owners.take(B, db.owners.CART, CART, "owner-b")
    wrong = FakePlatformWithLookup()  # B-like platform that holds nothing for this cart
    out = await rec.reconcile(wrong, A, CART, force=True)
    check("A's row, checked against a platform that holds nothing: stays blocked (too early), not settled", out["status"] == "absent_too_early" and (await ledger.payment_row(A, CART))["state"] == "UNKNOWN_OUTCOME", out)
    out = await rec.reconcile(pa, A, CART, force=True)
    check("A's row, checked against A's own platform: paid", out["status"] == "paid", out)
    ref = pa.purchases[0]["ref"]
    check("the order is owned by A's cart owner and by nobody on B", await db.owners.owner_of(A, db.owners.ORDER, ref) == "owner-a" and await db.owners.owner_of(B, db.owners.ORDER, ref) is None)
    check("B's row was never touched by A's reconciliation", (await ledger.payment_row(B, CART))["order_id"] == pb.purchases[0]["ref"])

    print("\n3. An operator resolving under the wrong connection finds nothing")
    pa2 = FakePlatformWithLookup(["charge_then_timeout"])
    await payment_guard.pay_basket(pa2, A, "ONLY-ON-A", "1111")
    res = await rec.resolve(pa2, B, "ONLY-ON-A", resolution="not_charged", order_id=None, note="")
    check("resolve for the other connection: not_found, and A's row is untouched", res["status"] == "not_found" and (await ledger.payment_row(A, "ONLY-ON-A"))["state"] == "UNKNOWN_OUTCOME", res)

    print("\n4. The operator list carries connection ids, reason codes and times - no shopper identity, no platform text")
    total, rows = await ledger.unresolved_payments()
    row = next(r for r in rows if r["cart_id"] == "ONLY-ON-A")
    flat = repr(row)
    check("an unresolved row appears for its own connection only", row["connection_id"] == A and all(r["connection_id"] != B or r["cart_id"] != "ONLY-ON-A" for r in rows), row)
    check("no owner key, session id or platform text in the listing", "owner-" not in flat and "RAW-PLATFORM-SECRET-TEXT" not in flat and "session" not in flat.lower(), flat)

    print("\n5. Keys cannot collide across merchants")
    check("the ledger key includes the connection", ledger.cart_payment_key(A, CART) != ledger.cart_payment_key(B, CART))


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
