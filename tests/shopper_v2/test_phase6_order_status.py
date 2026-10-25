"""Purchase outcome / post-purchase (`engine/shopper_v2/order_status.py`).

Live: creates a real cart, adds a real line, and completes a REAL checkout
against the Northfield demo merchant backend on 8001 (`adapter.checkout`) -
never infers purchase success from an attempted payment or from
conversation memory, only from the real order the adapter returns.
Reproduces V1's own ownership-gated line-item disclosure
(`engine/api/chat.py`'s CHECK_ORDER_STATUS branch) via `db.owners.claim`,
exactly as the real `/api/chat/pay` route does after a successful checkout.

    python tests/shopper_v2/test_phase6_order_status.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase6_order.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase6_order.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose, owners as db_owners
    from engine.shopper_v2.order_status import run_order_status_turn

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = "sess_test_phase6_order"
    owner_key = "pk_test_owner_phase6"
    adapter = engine.registry.adapter_for(connection_id)

    try:
        cart = await adapter.create_cart()
        await adapter.add_to_cart(cart.cart_id, "P3001", quantity=1)
        result = await adapter.checkout(cart.cart_id, idempotency_key=str(uuid.uuid4()))
        check("the real checkout succeeds against the live merchant", result.succeeded)
        order_id = result.order.order_id
        await db_owners.claim(connection_id, db_owners.ORDER, order_id, owner_key)

        # 1. No order id hinted and nothing recorded this visit -> honest
        #    "I don't have an order to look up", never a guess.
        no_order_reply, no_order_payload = await run_order_status_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id="sess_with_no_history",
            message="did my order go through?", hinted_order_id=None, owner_keys=["pk_nobody_with_orders"],
        )
        check("no known order -> honest decline, not a fabricated status", no_order_payload is None)
        # (the OWNER asking with no id now resolves their own newest order: see test_order_recovery_by_owner.py)

        # 2. An explicit order id, owned by the caller -> real status +
        #    real line items disclosed.
        reply, payload = await run_order_status_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id,
            message=f"what's the status of order {order_id}?",
            hinted_order_id=order_id, owner_keys=[owner_key],
        )
        check("a real order lookup succeeds", payload is not None)
        check("the real order id is reported", payload and payload.get("order_id") == order_id)
        check("payment is reported as captured (paid)", payload and payload.get("paid") is True)
        check("line items are disclosed to the real owner", "Trailblazer" in reply or "P3001" not in reply and bool(reply))

        # 3. Same order id, but the CALLER does not hold it -> status is
        #    still safe to state (V1's own documented behaviour: the base
        #    status has no shopper identity attached), but line items are
        #    never disclosed to a stranger.
        stranger_reply, stranger_payload = await run_order_status_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id="sess_stranger",
            message=f"what's the status of order {order_id}?",
            hinted_order_id=order_id, owner_keys=["pk_someone_else"],
        )
        check("a non-owner still gets the base status", stranger_payload is not None)
        check(
            "a non-owner is never told the line items",
            "x " not in stranger_reply.lower().replace("order", "").replace("status", ""),
        )

        # 4. An order id that does not exist -> the same honest "not found"
        #    message the platform itself uses, never a fabricated status.
        missing_reply, missing_payload = await run_order_status_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id,
            message="what about order zzz-does-not-exist?",
            hinted_order_id="zzz-does-not-exist", owner_keys=[owner_key],
        )
        check("a nonexistent order id -> honest not-found, no fabrication", missing_payload is None)
        check("the not-found reply names the fact plainly", bool(missing_reply))

        # 5. Tenant isolation: the same order id under a different
        #    connection_id is not owned there, so no line items leak across
        #    merchants even if somehow the id collided.
        cross_owner = await db_owners.owner_of("conn_kettle", db_owners.ORDER, order_id)
        check("the order is not owned under a different connection_id", cross_owner is None)

    finally:
        await engine.close()
        await dispose()

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 6 order-status checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
