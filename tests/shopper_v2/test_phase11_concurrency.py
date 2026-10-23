"""Concurrency test for the Focus Stack persistence path
(engine/db/shopping_state.py::merge). Proves the existing per-(session_id,
connection_id) asyncio.Lock actually prevents a lost update when two turns
for the SAME session race to read-compute-write `focus_stack` concurrently
- the exact risk named in the architecture review (a stack shape is a
single fact about the whole structure, not independently mergeable
fields, so a naive read-modify-write race could silently drop a push).

Run directly: python -m tests.shopper_v2.test_phase11_concurrency
"""
from __future__ import annotations

import asyncio
import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase11.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase11.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine.db import shopping_state as store  # noqa: E402
from engine.db.session import create_schema, dispose  # noqa: E402
from engine.shopper_v2.state import Topic, load  # noqa: E402

_checks = []


def check(label: str, ok: bool) -> None:
    _checks.append((label, ok))
    print(("  ok      " if ok else "  FAIL    ") + label)


async def _racing_push(session_id: str, connection_id: str, topic_label: str) -> None:
    """Simulates one whole TURN's read-compute-write cycle exactly as
    `engine/shopper_v2/graph.py::run_turn_via_graph` now does: hold the
    same per-session lock for the ENTIRE read-mutate-write span, not just
    the final `merge()` call. Read the current focus_stack, append a new
    topic to it (a real push, computed from what was actually read),
    write back. If the lock did not serialize the WHOLE span against a
    concurrent call, two overlapping calls would both read the SAME
    starting stack and each write back a stack containing only their OWN
    new topic, losing the other's - exactly the gap this test exists to
    catch (see this module's own docstring)."""
    async with store.lock_for(session_id, connection_id):
        state = await load(session_id, connection_id)
        new_stack = list(state.focus_stack)
        new_stack.append(
            Topic(topic_id=uuid.uuid4().hex[:12], kind="discovery", label=topic_label, state="paused")
        )
        # A real-world race needs a gap between read and write for two
        # UNLOCKED calls to actually overlap - reproduces the same window
        # a genuine two-tab/retry race would hit. With the lock held for
        # the whole span (above), this sleep now happens safely INSIDE
        # the critical section instead of exposing a race.
        await asyncio.sleep(0.05)
        await store.merge(
            session_id, connection_id,
            focus_stack=[t.model_dump(mode="json") for t in new_stack],
        )


async def main() -> bool:
    await create_schema()
    session_id = f"sess_concurrency_{uuid.uuid4().hex[:8]}"
    connection_id = "conn_demo"

    # 10 concurrent "turns" for the SAME session, each pushing its own
    # distinctly-labeled topic - mirrors the exact scenario the lock's own
    # docstring cites (10 concurrent additions previously lost 8 of them
    # before the lock existed).
    await asyncio.gather(*[
        _racing_push(session_id, connection_id, f"topic_{i}") for i in range(10)
    ])

    final_state = await load(session_id, connection_id)
    labels = {t.label for t in final_state.focus_stack}
    expected = {f"topic_{i}" for i in range(10)}
    check(
        f"all 10 concurrent pushes survived (got {len(labels)}/10, "
        f"missing: {sorted(expected - labels)})",
        labels == expected,
    )

    # Sequential (non-racing) sanity check: pushes done one at a time
    # must also all survive - the lock must not itself drop or reorder
    # anything for the ordinary, non-concurrent case.
    session_id2 = f"sess_concurrency_seq_{uuid.uuid4().hex[:8]}"
    for i in range(5):
        await _racing_push(session_id2, connection_id, f"seq_{i}")
    final_state2 = await load(session_id2, connection_id)
    labels2 = {t.label for t in final_state2.focus_stack}
    check("sequential (non-racing) pushes all survive too", labels2 == {f"seq_{i}" for i in range(5)})

    # Different sessions must never contend with or corrupt each other.
    session_a = f"sess_iso_a_{uuid.uuid4().hex[:8]}"
    session_b = f"sess_iso_b_{uuid.uuid4().hex[:8]}"
    await asyncio.gather(
        _racing_push(session_a, connection_id, "a_topic"),
        _racing_push(session_b, connection_id, "b_topic"),
    )
    state_a = await load(session_a, connection_id)
    state_b = await load(session_b, connection_id)
    check(
        "different sessions' concurrent writes never cross-contaminate",
        {t.label for t in state_a.focus_stack} == {"a_topic"}
        and {t.label for t in state_b.focus_stack} == {"b_topic"},
    )

    await dispose()
    if os.path.exists(_DB_FILE):
        os.remove(_DB_FILE)

    return all(ok for _, ok in _checks)


if __name__ == "__main__":
    ok = asyncio.run(main())
    print()
    print("all concurrency checks passed" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)
