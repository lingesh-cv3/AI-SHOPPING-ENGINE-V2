"""Proposal acceptance hardening (invariant-guard findings F1/F2).

F2: accepting a PENDING proposal must be an atomic claim - a REST accept-tap
    and a conversational "yes" (or a double tap) racing on the SAME proposal
    must execute exactly ONE cart change, never two.
F1: a model-claimed "confirmation" may execute a pending proposal only when it
    is the WHOLE message - a "yes" with a question/search/reference riding
    along must fall through to ordinary routing and leave the proposal PENDING.

Isolated sqlite; real Northfield backend (8001) for the real cart.

    python tests/shopper_v2/test_phase7_proposal_atomicity.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase7b.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_shopper_v2_phase7b.db"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class V:
    """Just the VerifiedInterpretation fields `_is_pure_confirmation` reads."""

    def __init__(self, **kw):
        self.question_type = "retrieve"
        self.cart_operation = "none"
        self.secondary_operation = None
        self.resolved_reference = None
        self.capability = "conversational"
        self.conversational_repair = "confirmation"
        self.__dict__.update(kw)


class Ref:
    def __init__(self, ids):
        self.product_ids = ids


class D:
    goal = None
    constraints_add = must_have_add = nice_to_have_add = exclusions_add = priorities_add = ()


async def main() -> None:
    import engine.shopper_v2.graph as graph_mod
    from engine.api.chat import accept_cart_proposal_core
    from engine.api.deps import build_engine
    from engine.db import cart_proposals as cp, create_schema, dispose
    from engine.shopper_v2.cart_proposal import run_cart_change_proposal_turn
    from engine.shopper_v2.state import ShoppingStateV2
    from engine.shopper_v2.tracing import TurnTrace
    from fastapi import HTTPException

    await create_schema()
    eng = build_engine()
    cid, sid = "conn_demo", "sess_atomic"
    adapter = eng.registry.adapter_for(cid)

    async def new_pending(product_id, operation="add"):
        cart = await adapter.create_cart()
        if operation != "add":
            await adapter.add_to_cart(cart.cart_id, product_id, quantity=1)
        st = ShoppingStateV2(session_id=sid, connection_id=cid, active_products=[product_id])
        _, proposal = await run_cart_change_proposal_turn(
            decision=eng.decision, gate=eng.gate, execution=eng.execution,
            connection_id=cid, session_id=sid, cart_id=cart.cart_id,
            message="x", state=st, operation=operation, hinted_product_id=product_id,
        )
        return cart.cart_id, proposal["proposal_id"]

    try:
        print("F2: atomic claim")
        _, pid = await new_pending("P3002")
        wins = await asyncio.gather(*[cp.claim_pending(cid, pid, state="ACCEPTED") for _ in range(8)])
        check("of 8 concurrent claims exactly one wins", sum(wins) == 1)
        check("a claim on an already-decided proposal loses", await cp.claim_pending(cid, pid, state="ACCEPTED") is False)

        cart_id, pid = await new_pending("P3002")
        outcomes = await asyncio.gather(*[
            accept_cart_proposal_core(connection_id=cid, session_id=sid, cart_id=cart_id,
                                      proposal_id=pid, shopper_id=None)
            for _ in range(4)
        ], return_exceptions=True)
        succeeded = [o for o in outcomes if not isinstance(o, Exception)]
        refused = [o for o in outcomes if isinstance(o, HTTPException) and o.status_code == 409]
        cart = await adapter.get_cart(cart_id)
        check("4 racing accepts: exactly one executes", len(succeeded) == 1 and len(refused) == 3)
        check("the cart holds the item ONCE (quantity 1), not once per accept",
              len(cart.lines) == 1 and cart.lines[0].quantity == 1)

        print("F1: confirmation must be pure")
        check("a bare yes is pure", graph_mod._is_pure_confirmation(V(), D()))
        check("a yes tagged cart_action/add-free is still pure",
              graph_mod._is_pure_confirmation(V(capability="cart_action"), D()))
        check("yes + alternatives question is NOT pure",
              not graph_mod._is_pure_confirmation(V(question_type="alternatives"), D()))
        check("yes + recommendation question is NOT pure",
              not graph_mod._is_pure_confirmation(V(question_type="recommendation"), D()))
        check("yes + a resolved product reference is NOT pure",
              not graph_mod._is_pure_confirmation(V(resolved_reference=Ref(["P1"])), D()))
        check("yes + a different cart operation is NOT pure",
              not graph_mod._is_pure_confirmation(V(cart_operation="remove"), D()))
        check("yes + a secondary operation is NOT pure",
              not graph_mod._is_pure_confirmation(V(secondary_operation=object()), D()))
        d = D(); d.constraints_add = ["waterproof"]
        check("yes + a new constraint is NOT pure", not graph_mod._is_pure_confirmation(V(), d))
        check("yes + a new search capability like compare is NOT pure",
              not graph_mod._is_pure_confirmation(V(capability="compare"), D()))

        cart_id, pid = await new_pending("P3002")
        proposal = await cp.get(cid, pid)
        state = {
            "trace": TurnTrace(), "new_state": ShoppingStateV2(session_id=sid, connection_id=cid),
            "connection_id": cid, "session_id": sid, "cart_id": cart_id, "shopper_id": None,
            "verified_interpretation": V(question_type="alternatives"),
            "pending_cart_proposal": proposal, "delta": D(),
        }
        out = await graph_mod.node_cart_proposal_continue(state)
        check("'yes, and what else do you have?' does not execute the proposal", out.get("result") is None)
        check("the proposal is still PENDING for a real confirmation",
              (await cp.get(cid, pid))["state"] == "PENDING")
        check("and the cart is untouched", len((await adapter.get_cart(cart_id)).lines) == 0)

        state["verified_interpretation"] = V()
        out = await graph_mod.node_cart_proposal_continue(state)
        check("a pure yes DOES execute it", out.get("result") is not None
              and len((await adapter.get_cart(cart_id)).lines) == 1)
    finally:
        from engine.api.deps import engine as module_engine

        await eng.close()
        await module_engine.close()
        await dispose()

    print()
    print(f"  {failures} FAILURE(S)" if failures else "  all proposal-atomicity checks passed")


asyncio.run(main())
sys.exit(1 if failures else 0)
