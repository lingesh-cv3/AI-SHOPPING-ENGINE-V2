"""No payment offer for a basket whose payment is held, in flight or paid (model-free; needs Northfield on 8001).

The V2 checkout turn replies with the real PREPARE_CHECKOUT dispatch. For a basket whose payment outcome is unknown (or in
flight, or already paid) it must not show a total with card buttons: it says why, in fixed words, and returns no payment payload.
A free basket and a declined one still get their offer. Nothing here charges anything; ledger states are written directly.

    python tests/shopper_v2/test_payment_hold_checkout_offer.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_pay_hold_offer.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_pay_hold_offer.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0
CONN = "conn_demo"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def checks(eng):
    from engine.db import idempotency as ledger
    from engine.shopper_v2.checkout import run_checkout_turn

    adapter = eng.registry.adapter_for(CONN)

    async def basket():
        c = await adapter.create_cart()
        await adapter.add_to_cart(c.cart_id, "P3001", quantity=1)  # a real line on the real demo platform
        return c.cart_id

    async def turn(cart_id):
        return await run_checkout_turn(
            decision=eng.decision, gate=eng.gate, execution=eng.execution, connection_id=CONN,
            session_id="sess_hold_offer", cart_id=cart_id, message="I'm ready to check out and pay",
        )

    print("1. A free basket still gets its offer")
    free = await basket()
    reply, payload = await turn(free)
    check("total and demo cards are offered", payload is not None and payload.get("cards") and "Card ending" in reply, reply[:140])

    print("\n2. A basket whose payment outcome is unknown gets no offer")
    held = await basket()
    claim = await ledger.begin_payment(CONN, held, attempt_key=f"pay-{held}-1111")
    await ledger.mark_unknown(CONN, held, token=claim["token"], reason="TIMEOUT")
    reply, payload = await turn(held)
    check("no payload, so no card buttons can be built from it", payload is None, payload)
    check("the reply says to wait and not to pay again, in fixed words", "don't pay again" in reply.lower() and "processed or needs checking" in reply, reply)
    check("it does not claim anything was or was not charged", "has been charged" not in reply.lower() and "nothing was charged" not in reply.lower(), reply)
    check("and it did not touch the cart", (await adapter.get_cart(held)).item_count == 1)

    print("\n3. A payment in flight gets no offer either")
    flying = await basket()
    await ledger.begin_payment(CONN, flying, attempt_key=f"pay-{flying}-1111")
    reply, payload = await turn(flying)
    check("in flight: no payload and the same hold wording", payload is None and "don't pay again" in reply.lower(), reply)

    print("\n4. A paid basket gets no offer")
    paid = await basket()
    claim = await ledger.begin_payment(CONN, paid, attempt_key=f"pay-{paid}-1111")
    await ledger.payment_settled(CONN, paid, succeeded=True, order_id="ORD-X", summary="paid ORD-X", token=claim["token"])
    reply, payload = await turn(paid)
    check("paid: no payload, says so", payload is None and "already been paid" in reply, reply)

    print("\n5. A declined basket is buyable again, so it gets its offer")
    declined = await basket()
    claim = await ledger.begin_payment(CONN, declined, attempt_key=f"pay-{declined}-0002")
    await ledger.payment_settled(CONN, declined, succeeded=False, order_id=None, summary="declined", token=claim["token"])
    reply, payload = await turn(declined)
    check("declined: offered again", payload is not None and payload.get("cards"), reply[:140])


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
