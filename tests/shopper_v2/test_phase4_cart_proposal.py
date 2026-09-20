"""Phase 4, first slice: cart proposal + approval + execution integration.

Uses its own isolated sqlite file (never cv3.db - see CLAUDE.md's database-
safety rules; this avoids any conflict with a concurrently-running live
engine process, which also touches cv3.db). Requires the Northfield demo
merchant backend running on 8001 (real HTTP, real read/write cart calls -
this test genuinely creates and mutates a cart on that backend, the same
demo/test cost `fuzz.py` already accepts). Does NOT require the engine
(port 8000) itself - this calls the same `engine.decision`/`engine.risk`/
`engine.execution` objects `engine.api.deps.build_engine()` constructs,
directly, the same way `engine/api/chat_v2.py` does.

    python tests/shopper_v2/test_phase4_cart_proposal.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase4.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase4.db")

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
    from engine.db import create_schema, dispose, cart_proposals as cp_store, record_case
    from engine.decision import operation_for
    from engine.shopper_v2.cart_proposal import run_cart_change_proposal_turn
    from engine.shopper_v2.state import ShoppingStateV2
    from shared.models import ActionType, ProposedAction

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = "sess_test_phase4"
    adapter = engine.registry.adapter_for(connection_id)

    try:
        cart = await adapter.create_cart()

        # 1. A proposal is created, and the cart is NOT touched yet.
        state = ShoppingStateV2(
            session_id=session_id, connection_id=connection_id, active_products=["P3001"]
        )
        reply, proposal = await run_cart_change_proposal_turn(
            decision=engine.decision,
            gate=engine.gate,
            execution=engine.execution,
            connection_id=connection_id,
            session_id=session_id,
            cart_id=cart.cart_id,
            message="add that to my cart",
            state=state,
            operation="add",
            hinted_product_id=None,
        )
        check("a proposal is created for a real, in-view product", proposal is not None)
        check("the proposal id is a real, non-empty string", bool(proposal and proposal.get("proposal_id")))
        cart_before = await adapter.get_cart(cart.cart_id)
        check("the cart is NOT mutated by proposing", len(cart_before.lines) == 0)

        stored = await cp_store.get(connection_id, proposal["proposal_id"])
        check("the proposal is stored as PENDING", stored is not None and stored["state"] == "PENDING")

        # 2. No cart_id -> honest decline, no proposal.
        no_cart_reply, no_cart_proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=None,
            message="add that to my cart", state=state, operation="add", hinted_product_id=None,
        )
        check("no cart_id -> honest decline, no proposal created", no_cart_proposal is None)

        # 3. No active product and no hint -> honest decline, no proposal.
        empty_state = ShoppingStateV2(session_id=session_id, connection_id=connection_id)
        no_product_reply, no_product_proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="add that to my cart", state=empty_state, operation="add", hinted_product_id=None,
        )
        check("no product in view -> honest decline, no proposal created", no_product_proposal is None)

        # 4. Approval (the same mechanics engine/api/chat.py::accept_proposal
        #    uses - decision -> risk -> execute_case) actually mutates the
        #    cart, and only after approval.
        add_action = ProposedAction(
            action_type=ActionType.ADD_TO_CART,
            operation=operation_for(ActionType.ADD_TO_CART),
            parameters={"chosen_product": stored["product_id"]},
        )
        trace2 = await engine.decision.decide([add_action], connection_id=connection_id)
        risk2 = engine.gate.classify(trace2.selected)
        check("ADD_TO_CART is auto-approved (non-financial)", str(risk2.outcome) == "AUTO")

        case_id2 = await record_case(
            connection_id=connection_id, friction=None, query="accept",
            cart_id=cart.cart_id, order_id=None, session_id=session_id,
            reasoning={"used_model": False, "model_name": None, "diagnosis": None,
                       "evidence": [], "fallback_reason": None, "model_reply": None,
                       "shopper_reply": None, "prompt_tokens": None, "completion_tokens": None},
            decision={
                "proposed": [{"action_type": str(add_action.action_type), "parameters": add_action.parameters,
                              "rationale": None, "confidence": None}],
                "rejected": [],
                "selected_action": str(trace2.selected.action.action_type),
                "selection_reason": trace2.selected.selection_reason,
            },
            risk={"outcome": str(risk2.outcome), "rule": risk2.policy_rule,
                  "reason": risk2.reason, "financial": risk2.properties.financial},
        )
        executed2 = await engine.execution.execute_case(connection_id, case_id2)
        check("the real ADD_TO_CART executes successfully", executed2.succeeded)

        cart_after = await adapter.get_cart(cart.cart_id)
        check("the cart now holds exactly the approved product", len(cart_after.lines) == 1 and cart_after.lines[0].product_id == "P3001")

        # 5. Idempotency: replaying the same case_id never double-adds.
        executed_retry = await engine.execution.execute_case(connection_id, case_id2)
        cart_after_retry = await adapter.get_cart(cart.cart_id)
        check("idempotency prevents a duplicate add on retry", len(cart_after_retry.lines) == 1)
        check("the retry itself still reports success (not an error)", executed_retry.succeeded)

        # 6. Tenant isolation: a proposal created under conn_demo is not
        #    visible/actionable under conn_kettle's own proposal store.
        kettle_lookup = await cp_store.get("conn_kettle", proposal["proposal_id"])
        check("a proposal is not visible under a different connection_id", kettle_lookup is None)

    finally:
        await engine.close()
        await dispose()

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 4 cart-proposal checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
