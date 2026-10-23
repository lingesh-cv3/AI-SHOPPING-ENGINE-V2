"""Phase 3 (PendingAction: CartProposal confirmation/cancellation/correction)
and Phase 5 (compound-operation routing fix: secondary_operation must
execute regardless of primary capability) - deterministic verification.

Uses its own isolated sqlite file (never cv3.db) and the real Northfield
demo merchant backend on 8001 for the real cart mutation checks (same
pattern as test_phase4_cart_proposal.py). The routing-gap fix is verified
with a monkeypatched graph invocation so every primary-capability shape can
be forced without needing each one's own full, realistic upstream state -
`_execute_secondary_operation` itself (the thing that actually runs) is
real, not mocked; only its `reasoning.synthesize` call is a test double,
exactly as Phase 5's original deterministic verification already did.

    python tests/shopper_v2/test_phase3_5_pending_and_routing.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase35.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase35.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


async def phase3() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose, cart_proposals as cp_store
    import engine.shopper_v2.graph as graph_mod
    from engine.shopper_v2.cart_proposal import run_cart_change_proposal_turn
    from engine.shopper_v2.state import ShoppingStateV2
    from engine.shopper_v2.tracing import TurnTrace
    

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = "sess_test_phase35"
    adapter = engine.registry.adapter_for(connection_id)

    try:
        # --- Confirmation: "yes" resolves and mutates the real cart ---
        cart = await adapter.create_cart()
        state = ShoppingStateV2(session_id=session_id, connection_id=connection_id, active_products=["P3001"])
        _, proposal = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart.cart_id,
            message="add that to my cart", state=state, operation="add", hinted_product_id="P3001",
        )
        check("setup: a real PENDING proposal exists", proposal is not None)

        fake_state = {
            "trace": TurnTrace(), "new_state": state, "connection_id": connection_id,
            "session_id": session_id, "cart_id": cart.cart_id, "shopper_id": None,
            "verified_interpretation": _FakeVerified(secondary_operation=None, capability="cart_action", repair="confirmation"),
            "pending_cart_proposal": proposal,
        }
        out = await graph_mod.node_cart_proposal_continue(fake_state)
        check("confirmation produces a terminal result", out.get("result") is not None)
        cart_after = await adapter.get_cart(cart.cart_id)
        check("confirmation actually mutates the real cart", len(cart_after.lines) == 1 and cart_after.lines[0].product_id == "P3001")
        stored = await cp_store.get(connection_id, proposal["proposal_id"])
        check("the proposal is now EXECUTED, not PENDING", stored["state"] == "EXECUTED")

        # --- Cancellation: "actually don't" rejects, no mutation ---
        cart2 = await adapter.create_cart()
        state2 = ShoppingStateV2(session_id=session_id, connection_id=connection_id, active_products=["P3002"])
        _, proposal2 = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart2.cart_id,
            message="add that to my cart", state=state2, operation="add", hinted_product_id="P3002",
        )
        fake_state2 = {
            "trace": TurnTrace(), "new_state": state2, "connection_id": connection_id,
            "session_id": session_id, "cart_id": cart2.cart_id, "shopper_id": None,
            "verified_interpretation": _FakeVerified(secondary_operation=None, capability="cart_action", repair="cancellation"),
            "pending_cart_proposal": proposal2,
        }
        out2 = await graph_mod.node_cart_proposal_continue(fake_state2)
        check("cancellation produces a terminal result", out2.get("result") is not None)
        check("cancellation reply is honest, not a fabricated success", "won't" in out2["result"].reply.lower() or "no problem" in out2["result"].reply.lower())
        cart2_after = await adapter.get_cart(cart2.cart_id)
        check("cancellation never mutates the cart", len(cart2_after.lines) == 0)
        stored2 = await cp_store.get(connection_id, proposal2["proposal_id"])
        check("the proposal is now REJECTED", stored2["state"] == "REJECTED")

        # --- Correction: rejects the stale one, falls through (no cart mutation here) ---
        cart3 = await adapter.create_cart()
        state3 = ShoppingStateV2(session_id=session_id, connection_id=connection_id, active_products=["P3001"])
        _, proposal3 = await run_cart_change_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart3.cart_id,
            message="add that to my cart", state=state3, operation="add", hinted_product_id="P3001",
        )
        fake_state3 = {
            "trace": TurnTrace(), "new_state": state3, "connection_id": connection_id,
            "session_id": session_id, "cart_id": cart3.cart_id, "shopper_id": None,
            "verified_interpretation": _FakeVerified(secondary_operation=None, capability="cart_action", repair="correction"),
            "pending_cart_proposal": proposal3,
        }
        out3 = await graph_mod.node_cart_proposal_continue(fake_state3)
        check("correction does NOT short-circuit with its own result (falls through)", out3.get("result") is None)
        stored3 = await cp_store.get(connection_id, proposal3["proposal_id"])
        check("correction rejects the stale proposal rather than leaving it PENDING", stored3["state"] == "REJECTED")
        cart3_after = await adapter.get_cart(cart3.cart_id)
        check("correction never mutates the cart by itself", len(cart3_after.lines) == 0)

        # --- No pending action -> honest fall-through, never invents one ---
        edge_state = {"new_state": ShoppingStateV2(session_id=session_id, connection_id=connection_id),
                      "friction": None, "pending_cart_proposal": None,
                      "decision": engine.decision, "gate": engine.gate, "execution": engine.execution}
        check("no pending proposal -> edge routes to ordinary topic_transition", graph_mod.edge_after_apply_delta(edge_state) == "topic_transition")

    finally:
        await engine.close()
        await dispose()




async def phase5_routing() -> None:
    import engine.shopper_v2.graph as graph_mod
    from engine.reasoning.service import SecondaryOperation, Synthesis
    from engine.shopper_v2.retrieval import RetrievalResult
    from engine.shopper_v2.state import ShoppingStateV2
    from engine.shopper_v2.tracing import TurnTrace
    from engine.shopper_v2.graph import TurnResultV2
    from engine.shopper_v2.evidence import TurnEvidence
    from shared.models.commerce import Money, Product

    trailblazer = Product(
        product_id="P1001", title="Trailblazer Running Shoe",
        price=Money(amount=4299, currency="INR"), availability="IN_STOCK",
        description="lightweight everyday trainer",
    )
    waterproof = Product(
        product_id="P2001", title="Highridge Waterproof Trail Running Shoe",
        price=Money(amount=8799, currency="INR"), availability="IN_STOCK",
        description="waterproof trail shoe, breathable membrane",
    )

    async def fake_retrieve(adapter, query, *, limit=5):
        return RetrievalResult(products=[trailblazer], unavailable=False)

    graph_mod.retrieve = fake_retrieve

    async def fake_fetch_products(adapter, ids):
        return [waterproof]

    graph_mod._fetch_products = fake_fetch_products

    fake_reasoning = AsyncMock()
    fake_reasoning.synthesize = AsyncMock(return_value=Synthesis(
        text="The Highridge is a genuine waterproof trail shoe; the Trailblazer is not.",
        cited_reason="waterproof trail shoe, breathable membrane", used_model=True,
    ))

    secondary_op = SecondaryOperation(capability="compare", reference_names=["Trailblazer"])
    state = ShoppingStateV2(session_id="s1", connection_id="conn_demo", active_products=["P2001"])

    primary_capabilities = ["state_change", "answer_question", "mission", "discover", "recommendation"]
    for cap in primary_capabilities:
        verified = _FakeVerified(secondary_operation=secondary_op, capability=cap)
        # Primary result has NO products of its own for state_change/answer_question
        # (the exact live-observed shape of the bug) - forces the fallback path.
        primary_products = [] if cap in ("state_change", "answer_question") else [waterproof]
        result = TurnResultV2(
            reply=f"[{cap} primary reply]", state=state,
            evidence=TurnEvidence(query=None, products=[]),
            used_model=False, trace={}, products=primary_products,
        )
        final_state = {
            "new_state": state, "verified_interpretation": verified, "result": result,
        }

        async def fake_ainvoke(initial, _final_state=final_state):
            return _final_state

        graph_mod._COMPILED_GRAPH.ainvoke = fake_ainvoke

        out = await graph_mod.run_turn_via_graph(
            reasoning=fake_reasoning, adapter=object(), session_id="s1",
            connection_id="conn_demo", message="whatever this turn's message was",
            cart_id=None,
        )
        check(
            f"secondary operation executes when primary capability = {cap!r}",
            "waterproof trail shoe" in out.reply.lower() or "highridge" in out.reply.lower(),
        )


class _FakeVerified:
    """Mirrors the fields of the real `VerifiedInterpretation` the graph reads."""

    def __init__(self, *, secondary_operation, capability, repair="none"):
        self.secondary_operation = secondary_operation
        self.capability = capability
        self.conversational_repair = repair
        self.question_type = "retrieve"
        self.cart_operation = "none"
        self.resolved_reference = None
        self.resolved_secondary = None
        self.needs_clarification = False
        self.catalog_categories = []


async def main() -> None:
    print("Phase 3: PendingAction (CartProposal confirmation/cancellation/correction)")
    await phase3()
    print()
    print("Phase 5: compound-operation routing (secondary_operation must not be dropped)")
    await phase5_routing()
    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 3 + Phase 5 checks passed")
    print()
    # Importing `graph` imports `engine.api.deps`, whose module-level engine
    # singleton owns live HTTP clients. Close it before this event loop dies,
    # or its transports are garbage-collected against a closed loop and the
    # interpreter crashes at exit (Windows proactor `__del__` access violation).
    from engine.api.deps import engine as _module_engine
    await _module_engine.close()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
