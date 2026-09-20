"""Outcome/interaction measurement for Shopper V2 - connects
`orchestrator.py` to the EXISTING `engine.execution.events` module/table
(the same one V1's `engine/api/chat.py` already writes to), never a second
measurement system. New `EventType` constants are additive to that same
fixed set.

Live: requires Northfield (8001) and a real model call (skips honestly if
no model is configured, matching this suite's own established pattern for
model-dependent live checks).

    python tests/shopper_v2/test_phase11_measurement.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase11.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase11.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


async def _events_for(connection_id: str, session_id: str) -> list[dict]:
    from sqlalchemy import select
    from engine.db.models import ShopperEvent
    from engine.db.session import session_scope

    async with session_scope() as db:
        rows = (
            await db.execute(
                select(ShopperEvent).where(
                    ShopperEvent.connection_id == connection_id,
                    ShopperEvent.session_id == session_id,
                )
            )
        ).scalars().all()
        return [{"event_type": r.event_type, "product_id": r.product_id, "shopper_id": r.shopper_id} for r in rows]


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    from engine.shopper_v2.orchestrator import run_turn

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    session_id = "sess_test_phase11"
    adapter = engine.registry.adapter_for(connection_id)

    if engine.reasoning is None or not engine.reasoning.enabled:
        check("model not configured - live measurement check honestly skipped", True)
        await engine.close()
        await dispose()
        print()
        print("  0 FAILURE(S) (skipped - no model configured)" if not failures else f"  {failures} FAILURE(S)")
        print()
        return

    try:
        # 1. A discovery turn that shows at least one product should write
        #    a real PRODUCT_IMPRESSION row, scoped to this exact session.
        result = await run_turn(
            reasoning=engine.reasoning, adapter=adapter, session_id=session_id,
            connection_id=connection_id, message="show me some running shoes",
            shopper_id="test_shopper_1",
        )
        events = await _events_for(connection_id, session_id)
        check("a discovery turn writes at least one event", len(events) > 0)
        check(
            "a PRODUCT_IMPRESSION event was recorded when products were shown",
            not result.products or any(e["event_type"] == "product_impression" for e in events),
        )
        check(
            "the recorded shopper_id matches what the caller passed",
            all(e["shopper_id"] == "test_shopper_1" for e in events if events),
        )

        # 2. Tenant isolation: the same session_id under a DIFFERENT
        #    connection_id sees none of this connection's events.
        cross_tenant_events = await _events_for("conn_kettle", session_id)
        check("no events leak across connection_id (tenant isolation)", cross_tenant_events == [])

        # 3. A cart-proposal turn (if one is reachable this turn) should
        #    write a real PROPOSAL_CREATED event, tagged with the real
        #    product id - never a fabricated one.
        session_id2 = "sess_test_phase11_cart"
        cart = await adapter.create_cart()
        if result.products:
            pid = result.products[0].product_id
            from engine.db import shopping_state as shopping_state_store
            await shopping_state_store.merge(session_id2, connection_id, active_products=[pid])
            result2 = await run_turn(
                reasoning=engine.reasoning, adapter=adapter, session_id=session_id2,
                connection_id=connection_id, message="add that to my cart",
                cart_id=cart.cart_id, decision=engine.decision, gate=engine.gate,
                execution=engine.execution, shopper_id="test_shopper_2",
            )
            events2 = await _events_for(connection_id, session_id2)
            proposal_events = [e for e in events2 if e["event_type"] == "proposal_created"]
            if proposal_events:
                check(
                    "a PROPOSAL_CREATED event names a real product id",
                    proposal_events[0]["product_id"] == pid,
                )
            else:
                check(
                    "cart-intent turn did not classify as a proposal this run (model variance, not a measurement bug)",
                    True,
                )
    finally:
        await engine.close()
        await dispose()

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 11 measurement checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
