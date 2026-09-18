"""ShoppingStateV2 transition semantics - the exact multi-turn examples from
both Shopper V2 research docs (budget narrowing, priority reordering).

Uses its own isolated sqlite file (never cv3.db - see CLAUDE.md's database
safety rules) so this can be run any number of times without touching real
data.

    python tests/shopper_v2/test_state_transitions.py
"""

import asyncio
import os
import sys

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_state.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


async def main() -> None:
    from engine.db import create_schema, dispose
    from engine.shopper_v2.state import apply_delta, load
    from engine.shopper_v2.understanding import StateDelta

    await create_schema()

    session_id = "sess_test_state_v2"
    connection_id = "conn_demo"

    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
        if not ok:
            failures += 1

    # Turn 1: "I need running shoes."
    state = await load(session_id, connection_id)
    state = await apply_delta(state, StateDelta(goal="running shoes"))
    check("turn 1 sets goal", state.goal == "running shoes")
    check("turn 1 version increments", state.version == 1)

    # Turn 2: "Under 2000." (stored as the existing V1 `merge()` already
    # stores it - `str(float(...))` - not re-derived here.)
    state = await apply_delta(state, StateDelta(budget_amount="2000"))
    check("turn 2 sets budget", state.budget_amount == "2000.0")
    check("turn 2 preserves goal", state.goal == "running shoes")
    check("turn 2 version increments", state.version == 2)

    # Turn 3: "Lightweight."
    state = await apply_delta(state, StateDelta(constraints_add=["lightweight"]))
    check("turn 3 adds constraint", "lightweight" in state.constraints)

    # Turn 4: "Actually keep it under 1500." - supersedes, does not duplicate.
    state = await apply_delta(state, StateDelta(budget_amount="1500"))
    check("turn 4 supersedes budget (not duplicated)", state.budget_amount == "1500.0")

    # Turn 5: "Comfort is more important." then "No, durability is more important."
    state = await apply_delta(state, StateDelta(priorities_add=["comfort"]))
    state = await apply_delta(state, StateDelta(priorities_add=["durability"]))
    check(
        "priorities are additive, most recent last",
        state.priorities == ["comfort", "durability"],
    )
    check("mission (goal) survives priority changes", state.goal == "running shoes")

    # Explicit clear: "Budget doesn't matter anymore."
    state = await apply_delta(state, StateDelta(clear_budget=True))
    check("explicit clear nulls budget_amount", state.budget_amount is None)
    check("clear does not touch goal", state.goal == "running shoes")

    # A no-op delta still returns a valid, unincremented-in-content state.
    before_goal = state.goal
    state = await apply_delta(state, StateDelta())
    check("no-op delta leaves goal untouched", state.goal == before_goal)

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all state-transition checks passed")
    print()

    await dispose()
    sys.exit(1 if failures else 0)


asyncio.run(main())
