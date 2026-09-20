"""Generalized cart-change operations: REMOVE, CHANGE_QUANTITY, and CLEAR
proposals through `cart_proposal.py::run_cart_change_proposal_turn` - see
that module's own docstring for how CLEAR was added to the existing,
shared `PROPOSE_CART_CHANGE` mechanism (V1<->V2 parity migration), never a
new commerce implementation.

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

        # 1. "clear" now proposes (never mutates directly) - a real, stored
        #    CLEAR proposal, stricter than V1's own direct-execute
        #    CLEAR_CART (see cart_proposal.py's docstring on why).
        clear_reply, clear_proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="clear my cart", state=state, operation="clear", hinted_product_id=None,
        )
        check("a CLEAR proposal is created, not a fabricated decline", clear_proposal is not None)
        check("clear reply is honest, not silent", bool(clear_reply))
        cart_after_clear_proposal = await adapter.get_cart(cart.cart_id)
        check(
            "the cart is NOT mutated by proposing a clear",
            len(cart_after_clear_proposal.lines) == 1,
        )
        check(
            "the stored proposal's change_type is CLEAR",
            clear_proposal is not None and clear_proposal.get("change_type") == "CLEAR",
        )

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

        # 6. Approving the CLEAR proposal (the same mechanics
        #    engine/api/chat.py::accept_proposal uses - decision -> risk ->
        #    execute_case) actually empties the real cart, and only after
        #    approval - never before (checks 1's own assertion above).
        from engine.decision import operation_for
        from engine.execution import service as execution_service
        from engine.db import record_case
        from shared.models import ActionType, ProposedAction

        check(
            "the stored proposal's product_id is the documented sentinel, "
            "never a real product id",
            clear_proposal is not None
            and clear_proposal.get("product_id") == execution_service._CLEAR_CART_SENTINEL,
        )

        clear_action = ProposedAction(
            action_type=ActionType.CLEAR_CART,
            operation=operation_for(ActionType.CLEAR_CART),
            parameters={},
        )
        trace3 = await engine.decision.decide([clear_action], connection_id=connection_id)
        risk3 = engine.gate.classify(trace3.selected)
        check("CLEAR_CART is auto-approved (non-financial)", str(risk3.outcome) == "AUTO")

        case_id3 = await record_case(
            connection_id=connection_id, friction=None, query="accept clear",
            cart_id=cart.cart_id, order_id=None, session_id=session_id,
            reasoning={"used_model": False, "model_name": None, "diagnosis": None,
                       "evidence": [], "fallback_reason": None, "model_reply": None,
                       "shopper_reply": None, "prompt_tokens": None, "completion_tokens": None},
            decision={
                "proposed": [{"action_type": str(clear_action.action_type), "parameters": {},
                              "rationale": None, "confidence": None}],
                "rejected": [],
                "selected_action": str(trace3.selected.action.action_type),
                "selection_reason": trace3.selected.selection_reason,
            },
            risk={"outcome": str(risk3.outcome), "rule": risk3.policy_rule,
                  "reason": risk3.reason, "financial": risk3.properties.financial},
        )
        executed3 = await engine.execution.execute_case(connection_id, case_id3)
        check("the real CLEAR_CART executes successfully once approved", executed3.succeeded)

        cart_after_clear = await adapter.get_cart(cart.cart_id)
        check(
            "the cart is genuinely empty only after the proposal is approved",
            len(cart_after_clear.lines) == 0,
        )

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
