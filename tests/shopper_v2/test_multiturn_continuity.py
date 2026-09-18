"""The 7-turn multi-turn continuity example from both Shopper V2 research
docs, exercised against `engine.shopper_v2.state` directly (the state layer
- not a live LLM call, since Understanding's model output is exactly what
`StateDelta` already models and is covered by test_state_transitions.py;
this test's job is to prove the SAME state object accumulates correctly
across a realistic full sequence, not to re-test the model).

Uses its own isolated sqlite file - never cv3.db.

    python tests/shopper_v2/test_multiturn_continuity.py
"""

import os
import sys

os.environ.setdefault(
    "DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_multiturn.db"
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402


async def main() -> None:
    from engine.db import create_schema, dispose
    from engine.shopper_v2.state import apply_delta, load
    from engine.shopper_v2.understanding import StateDelta

    await create_schema()

    session_id = "sess_test_multiturn_v2"
    connection_id = "conn_demo"
    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
        if not ok:
            failures += 1

    state = await load(session_id, connection_id)

    # Turn 1: I need running shoes.
    state = await apply_delta(state, StateDelta(goal="running shoes"))
    # Turn 2: Under 2000.
    state = await apply_delta(state, StateDelta(budget_amount="2000"))
    # Turn 3: Lightweight.
    state = await apply_delta(state, StateDelta(constraints_add=["lightweight"]))
    # Turn 4: Actually comfort matters more.
    state = await apply_delta(state, StateDelta(priorities_add=["comfort"]))
    # Turn 5: No, durability matters more.
    state = await apply_delta(state, StateDelta(priorities_add=["durability"]))
    check(
        "priorities accumulate in stated order across 5 turns",
        state.priorities == ["comfort", "durability"],
    )
    check("mission (goal) survived 5 turns of refinement", state.goal == "running shoes")
    check("budget survived 5 turns of refinement", state.budget_amount == "2000.0")
    check("constraint survived 5 turns of refinement", "lightweight" in state.constraints)

    # Turn 6: Set an active shortlist (what a real turn's retrieval would do
    # - active_products is replaced wholesale, unlike the additive lists).
    state = await apply_delta(state, StateDelta())  # no-op, simulates a pure-read turn
    check("turn 6 (pure read) does not reset the mission", state.goal == "running shoes")

    # Turn 7: mission status changes as the shopper moves toward a decision.
    state = await apply_delta(state, StateDelta(mission_status="comparing"))
    check("mission_status updates without resetting other fields", state.mission_status == "comparing")
    check("goal still intact at turn 7", state.goal == "running shoes")
    check("version reflects every turn applied", state.version == 7)

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all multi-turn continuity checks passed")
    print()

    await dispose()
    sys.exit(1 if failures else 0)


asyncio.run(main())
