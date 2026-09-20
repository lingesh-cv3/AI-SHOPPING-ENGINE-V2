"""Generalized cart-change operations: REMOVE and CHANGE_QUANTITY proposals
through `cart_proposal.py::run_cart_change_proposal_turn`, plus the honest
"clear" decline (see cart_proposal.py's own docstring for why CLEAR_CART is
not routed through the reused proposal mechanism).

Live: requires the Northfield demo merchant backend on 8001 (creates and
mutates a real cart). Does not require the engine (port 8000) - calls
`engine.decision`/`engine.risk`/`engine.execution` directly, exactly as
`engine/api/chat_v2.py` does.

    python tests/shopper_v2/test_phase6_cart_change_operations.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase6.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase6.db")

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
    from engine.db import create_schema, dispose
    from engine.shopper_v2.cart_proposal import run_cart_change_proposal_turn
    from engine.shopper_v2.state import ShoppingStateV2

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = "sess_test_phase6"
    adapter = engine.registry.adapter_for(connection_id)

    try:
        cart = await adapter.create_cart()
        await adapter.add_to_cart(cart.cart_id, "P3001", quantity=1)

        state = ShoppingStateV2(session_id=session_id, connection_id=connection_id)

        # 1. "clear" is an honest, explicit decline - never a fabricated mutation.
        clear_reply, clear_proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="clear my cart", state=state, operation="clear", hinted_product_id=None,
        )
        check("clear is explicitly not supported - no proposal fabricated", clear_proposal is None)
        check("clear reply is honest, not silent", bool(clear_reply))
        cart_after_clear_attempt = await adapter.get_cart(cart.cart_id)
        check("the real cart is untouched by the clear decline", len(cart_after_clear_attempt.lines) == 1)

        # 2. REMOVE proposal is created, cart not yet mutated.
        remove_reply, remove_proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="remove that", state=state, operation="remove", hinted_product_id="P3001",
        )
        check("a REMOVE proposal is created for a real cart line", remove_proposal is not None)
        cart_after_remove_proposal = await adapter.get_cart(cart.cart_id)
        check("the cart is NOT mutated by proposing a removal", len(cart_after_remove_proposal.lines) == 1)

        # 3. CHANGE_QUANTITY proposal is created, cart not yet mutated.
        qty_reply, qty_proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="make that quantity two", state=state, operation="set_quantity",
            hinted_product_id="P3001", quantity=2,
        )
        check("a CHANGE_QUANTITY proposal is created", qty_proposal is not None)
        cart_after_qty_proposal = await adapter.get_cart(cart.cart_id)
        check(
            "the cart quantity is NOT mutated by proposing a change",
            cart_after_qty_proposal.lines[0].quantity == 1,
        )

        # 4. No target -> honest decline, no proposal (never a guess).
        no_target_reply, no_target_proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="remove that", state=state, operation="remove", hinted_product_id=None,
        )
        check("no resolved target for remove -> honest decline, no proposal", no_target_proposal is None)

        # 5. No cart_id -> honest decline regardless of operation.
        no_cart_reply, no_cart_proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=None,
            message="remove that", state=state, operation="remove", hinted_product_id="P3001",
        )
        check("no cart_id -> honest decline, no proposal created", no_cart_proposal is None)

    finally:
        await engine.close()
        await dispose()

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 6 cart-change-operations checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
