"""Multi-item basket proposals (`engine/shopper_v2/basket_proposal.py`).

Live: requires the Northfield demo merchant backend on 8001 (creates and
reads a real cart, creates real `CartProposal` rows). Does not require the
engine (port 8000) - calls `engine.decision`/`engine.risk`/
`engine.execution` directly, exactly as `test_phase4_cart_proposal.py`
already does for the single-item path this one extends.

    python tests/shopper_v2/test_phase8_basket_proposal.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase8.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase8.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class _ScriptedReasoning:
    enabled = True

    def __init__(self, intent):
        self._intent = intent

    async def interpret_mission_items(self, **kwargs):
        return self._intent


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose, cart_proposals as cp_store
    from engine.reasoning.service import MissionItemIntent
    from engine.shopper_v2.basket_proposal import run_basket_proposal_turn
    from engine.shopper_v2.missions import run_mission_turn
    from engine.shopper_v2.state import MissionItem, ShoppingStateV2

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = "sess_test_phase8"
    adapter = engine.registry.adapter_for(connection_id)

    try:
        cart = await adapter.create_cart()

        # 1. Two verified items (one candidate each), one with no
        #    candidates, one with more than one (ambiguous) - all
        #    independently proposed/reported, never guessed.
        items = [
            MissionItem(item_id="i1", label="shoes", candidates=["P1001"]),
            MissionItem(item_id="i2", label="socks", candidates=["P3002"]),
            MissionItem(item_id="i3", label="hydration bottle", candidates=[]),
            MissionItem(item_id="i4", label="jacket", candidates=["P1002", "P1003"]),
            MissionItem(item_id="i5", label="belt", candidates=["P1005"], status="done"),
        ]
        state = ShoppingStateV2(
            session_id=session_id, connection_id=connection_id, mission_items=items,
        )

        reply, products, new_state, results = await run_basket_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            adapter=adapter, connection_id=connection_id, session_id=session_id,
            cart_id=cart.cart_id, message="add these to my cart", state=state,
        )

        by_item = {r.item.item_id: r for r in results}
        check("shoes item is proposed (single verified candidate)", by_item["i1"].outcome == "proposed")
        check("socks item is proposed (single verified candidate)", by_item["i2"].outcome == "proposed")
        check("hydration bottle is honestly reported as no candidates", by_item["i3"].outcome == "no_candidates")
        check("jacket (2 candidates) is honestly reported as ambiguous, never guessed", by_item["i4"].outcome == "ambiguous")
        check("an already-done item (belt) is skipped entirely", "i5" not in by_item)

        check("exactly 2 real proposals were created", sum(1 for r in results if r.outcome == "proposed") == 2)
        check("the reply names the proposed items", "shoes" in reply and "socks" in reply)
        check("the reply names the ambiguous item", "jacket" in reply)
        check("the reply names the empty item", "hydration bottle" in reply)

        # 2. The cart itself is NOT mutated by proposing - same invariant
        #    the single-item path already guarantees.
        cart_after = await adapter.get_cart(cart.cart_id)
        check("the real cart is untouched by basket proposing", len(cart_after.lines) == 0)

        # 3. Each created proposal is tagged with the RIGHT mission item id
        #    - never cross-wired, never a fabricated mapping.
        shoes_proposal = await cp_store.get(connection_id, by_item["i1"].proposal["proposal_id"])
        socks_proposal = await cp_store.get(connection_id, by_item["i2"].proposal["proposal_id"])
        check("the shoes proposal is tagged with the shoes item id", shoes_proposal["mission_item_id"] == "i1")
        check("the socks proposal is tagged with the socks item id", socks_proposal["mission_item_id"] == "i2")
        check(
            "the shoes proposal names the real shoes product, not the socks product",
            shoes_proposal["product_id"] == "P1001",
        )
        check(
            "the socks proposal names the real socks product, not the shoes product",
            socks_proposal["product_id"] == "P3002",
        )

        # 4. Both proposals are visible together via the EXISTING, unmodified
        #    pending-proposals listing - "the basket" is nothing but this.
        pending = await cp_store.pending_for_cart(connection_id, cart.cart_id, session_id=session_id)
        check("both basket proposals appear in the existing pending-proposals list", len(pending) == 2)

        # 5. State reflects the tentative picks, never silently substituted.
        shoes_state_item = next(i for i in new_state.mission_items if i.item_id == "i1")
        check("the shoes item's selected_product_id is the real proposed product", shoes_state_item.selected_product_id == "P1001")
        check("the shoes item's status advances to 'deciding'", shoes_state_item.status == "deciding")
        bottle_state_item = next(i for i in new_state.mission_items if i.item_id == "i3")
        check("the empty item's selected_product_id remains unset", bottle_state_item.selected_product_id is None)

        # 6. No cart_id -> honest decline, no proposals created at all.
        no_cart_reply, no_cart_products, no_cart_state, no_cart_results = await run_basket_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            adapter=adapter, connection_id=connection_id, session_id=session_id,
            cart_id=None, message="add these to my cart", state=state,
        )
        check("no cart_id -> honest decline, no results at all", no_cart_results == [])

        # 7. Everything already done -> honest "nothing to propose", no
        #    proposals fabricated for settled items.
        all_done_state = ShoppingStateV2(
            session_id=session_id, connection_id=connection_id,
            mission_items=[MissionItem(item_id="i9", label="done thing", status="done", candidates=["P1001"])],
        )
        done_reply, _p, _s, done_results = await run_basket_proposal_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            adapter=adapter, connection_id=connection_id, session_id=session_id,
            cart_id=cart.cart_id, message="add these to my cart", state=all_done_state,
        )
        check("an all-done mission proposes nothing", done_results == [])

        # 8. Full turn-level integration via run_mission_turn: the
        #    propose_basket signal now reaches `fulfillment.py` directly
        #    (architecture review, Step 7) - NOT `basket_proposal.py`'s
        #    older pending-confirm flow. P1005 ("Fell Runner Trail Shoe")
        #    genuinely needs a size choice (real catalog data, confirmed
        #    live against the running Northfield adapter before writing
        #    this assertion - CLAUDE.md's own "assert against real
        #    observed output" standard), so the correct new behavior is a
        #    real variant question, not a direct add and not a pending
        #    CartProposal row at all.
        turn_state = ShoppingStateV2(
            session_id="sess_test_phase8_turn", connection_id=connection_id,
            mission_items=[MissionItem(item_id="j1", label="running shoes", candidates=["P1005"])],
        )
        cart2 = await adapter.create_cart()
        reasoning = _ScriptedReasoning(MissionItemIntent(new_item_labels=[], propose_basket=True))
        turn_reply, turn_products, turn_new_state, turn_handled, turn_proposal_ids = await run_mission_turn(
            reasoning=reasoning, adapter=adapter, message="add these to my cart",
            history=None, state=turn_state,
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id=connection_id, session_id="sess_test_phase8_turn", cart_id=cart2.cart_id,
        )
        check("run_mission_turn handles a basket-propose request end to end", turn_handled)
        check(
            "run_mission_turn's reply asks the real variant question for the real product",
            "Fell Runner Trail Shoe" in turn_reply and "7" in turn_reply,
        )
        turn_pending = await cp_store.pending_for_cart(connection_id, cart2.cart_id, session_id="sess_test_phase8_turn")
        check(
            "no pending CartProposal is created - this path adds directly, "
            "never through a separate confirm step",
            turn_pending == [],
        )
        check(
            "no proposal ids are surfaced either - nothing was proposed",
            turn_proposal_ids == [],
        )
        check(
            "the fulfillment queue now holds the real pending variant question",
            any(i.product_id == "P1005" and i.status == "needs_variant" for i in turn_new_state.fulfillment_queue),
        )

        # 9. Without decision/gate/execution wired in, degrades honestly
        #    rather than silently dropping the request.
        no_engine_reply, _p2, _s2, no_engine_handled, _ids2 = await run_mission_turn(
            reasoning=reasoning, adapter=adapter, message="add these to my cart",
            history=None, state=turn_state,
        )
        check("missing decision/gate/execution degrades honestly, not silently", no_engine_handled)
        check("the degrade message says so plainly", bool(no_engine_reply))

    finally:
        await engine.close()
        await dispose()

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 8 basket-proposal checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
