"""The two real demo platforms, for payment safety (model-free; needs Northfield on 8001 and Kettle on 8002 running).

Northfield can say which purchases exist for a basket (`lookup_checkout`); Kettle cannot, and says so by not offering the
capability. Both stubs must really honour a repeated idempotency key even when two requests arrive at once - the claim
`honours_idempotency_keys` rests on that. Nothing here touches checkout through the engine or any real provider.

    python tests/shopper_v2/test_payment_platform_adapters.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_pay_adapters.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_pay_adapters.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402
import uuid  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def basket(adapter, product, variant):
    cart = await adapter.create_cart()
    await adapter.add_to_cart(cart.cart_id, product, variant_id=variant, quantity=1)
    return cart.cart_id


async def checks(eng):
    north = eng.registry.adapter_for("conn_demo")
    kettle = eng.registry.adapter_for("conn_kettle")

    print("1. Northfield: lookup_checkout")
    cid = await basket(north, "P1002", "P1002-8")
    none = await north.lookup_checkout(cid, [])
    check("a basket with no purchase: ABSENT (a complete answer)", none.status == "ABSENT" and none.orders == [], none)
    key = f"pay-{cid}-1111"
    first = await north.checkout_with_card(cid, card_last4="1111", idempotency_key=key)
    found = await north.lookup_checkout(cid, [key])
    check("after a purchase: FOUND with the platform's order, read as paid", found.status == "FOUND" and [o.order_id for o in found.orders] == [first.order.order_id]
          and str(found.orders[0].payment_status) in ("CAPTURED", "AUTHORIZED"), found)
    other = await basket(north, "P1002", "P1002-8")
    check("another basket's purchases are not returned (filtered by basket)", (await north.lookup_checkout(other, [])).status == "ABSENT")
    check("Northfield declares it honours idempotency keys", north.honours_idempotency_keys is True)

    print("\n2. Northfield: the same key twice AT ONCE creates one purchase")
    cid2 = await basket(north, "P1002", "P1002-8")
    k2 = f"pay-{cid2}-1111"
    a, b = await asyncio.gather(
        north.checkout_with_card(cid2, card_last4="1111", idempotency_key=k2),
        north.checkout_with_card(cid2, card_last4="1111", idempotency_key=k2),
    )
    rows = await north.lookup_checkout(cid2, [k2])
    check("two simultaneous requests, one key: one purchase, both told the same order", a.order.order_id == b.order.order_id and len(rows.orders) == 1, (a.order.order_id, b.order.order_id, len(rows.orders)))

    print("\n3. Kettle: no lookup, but the same key is honoured under concurrency")
    check("Kettle offers no lookup_checkout (its unknown outcomes need a person or the same-key retry)", not hasattr(kettle, "lookup_checkout"))
    check("Kettle declares it honours idempotency keys", kettle.honours_idempotency_keys is True)
    kc = await basket(kettle, "KB-BLD-05", "KB-BLD-05::250g ground")
    kk = f"pay-{kc}-1111"
    ka, kb = await asyncio.gather(
        kettle.checkout_with_card(kc, card_last4="1111", idempotency_key=kk),
        kettle.checkout_with_card(kc, card_last4="1111", idempotency_key=kk),
    )
    check("two simultaneous requests, one key: both told the same order", ka.order.order_id == kb.order.order_id, (ka.order.order_id, kb.order.order_id))


async def main():
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose

    await create_schema()
    eng = build_engine()
    try:
        await checks(eng)
    finally:
        await eng.close()
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
