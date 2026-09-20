"""Phase 5, first slice: Checkout continuity (`engine/shopper_v2/checkout.py`).

Uses its own isolated sqlite file (never cv3.db). Requires the Northfield
demo merchant backend running on 8001 (real HTTP cart calls). Does NOT
require the engine (port 8000) itself - calls the real
`engine.decision`/`engine.risk`/`engine.execution` objects directly, same
as `test_phase4_cart_proposal.py`.

    python tests/shopper_v2/test_phase5_checkout.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase5.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase5.db")

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
    from engine.db import create_schema, dispose, record_case
    from engine.decision import operation_for
    from engine.shopper_v2.checkout import run_checkout_turn
    from shared.models import ActionType, ProposedAction

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = "sess_test_phase5"
    adapter = engine.registry.adapter_for(connection_id)

    try:
        cart = await adapter.create_cart()

        # 1. No cart_id -> honest decline, no case recorded.
        reply0, payload0 = await run_checkout_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=None,
            message="I'd like to pay now",
        )
        check("no cart_id -> honest decline", payload0 is None and "cart" in reply0.lower())

        # 2. A cart exists but is empty, and the shopper DID ask to pay ->
        #    the existing dispatch's own empty-cart refusal, reused
        #    unmodified, not re-implemented here.
        reply1, payload1 = await run_checkout_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="I'm ready to pay",
        )
        check("empty cart -> honest decline, no payload", payload1 is None)
        check("empty cart reply names the real reason", "empty" in reply1.lower())

        # 3. The shopper did NOT mention paying at all -> the existing
        #    "did not offer a checkout" gate, reused unmodified.
        reply2, payload2 = await run_checkout_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="hi there",
        )
        check("no mention of paying -> checkout not offered", payload2 is None)

        # 4. Add a real line via the real pipeline (mirrors
        #    test_phase4_cart_proposal.py's own approach) so checkout has
        #    something real to show.
        add_action = ProposedAction(
            action_type=ActionType.ADD_TO_CART,
            operation=operation_for(ActionType.ADD_TO_CART),
            parameters={"chosen_product": "P3001"},  # Insulated Water Bottle, no variants
        )
        trace_add = await engine.decision.decide([add_action], connection_id=connection_id)
        risk_add = engine.gate.classify(trace_add.selected)
        case_add = await record_case(
            connection_id=connection_id, friction=None, query="add", cart_id=cart.cart_id,
            order_id=None, session_id=session_id,
            reasoning={"used_model": False, "model_name": None, "diagnosis": None, "evidence": [],
                       "fallback_reason": None, "model_reply": None, "shopper_reply": None,
                       "prompt_tokens": None, "completion_tokens": None},
            decision={"proposed": [{"action_type": str(add_action.action_type), "parameters": add_action.parameters,
                                     "rationale": None, "confidence": None}],
                      "rejected": [], "selected_action": str(trace_add.selected.action.action_type),
                      "selection_reason": trace_add.selected.selection_reason},
            risk={"outcome": str(risk_add.outcome), "rule": risk_add.policy_rule,
                  "reason": risk_add.reason, "financial": risk_add.properties.financial},
        )
        await engine.execution.execute_case(connection_id, case_add)

        # 5. Now checkout should succeed, grounded in the real cart.
        reply3, payload3 = await run_checkout_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="I'm ready to check out and pay",
        )
        check("a real, non-empty cart + a real payment mention -> a real payload", payload3 is not None)
        check("the payload names the real product", any(l["title"] == "Insulated Water Bottle 750ml" for l in (payload3 or {}).get("lines", [])))
        check("the reply states the real total, not a guess", str(payload3.get("grand_total")) in reply3)
        check("the reply never claims payment happened", "paid" not in reply3.lower() and "charged" not in reply3.lower())
        check("the reply names the demo cards honestly, not a real payment method", "Card ending" in reply3)

        # 6. The cart itself is untouched by checkout - still exactly one
        #    line, same as before this ran (PREPARE_CHECKOUT reads, never
        #    mutates).
        cart_after = await adapter.get_cart(cart.cart_id)
        check("checkout never mutates the cart", len(cart_after.lines) == 1)

    finally:
        await engine.close()
        await dispose()

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 5 checkout checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
