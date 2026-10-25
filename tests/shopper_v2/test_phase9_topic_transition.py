"""Exhaustive tests for engine/shopper_v2/topic_transition.py's five-rule
decision procedure - pure, synchronous, no live services required. Run
directly: python -m tests.shopper_v2.test_phase9_topic_transition
"""
from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

sys.path.insert(0, ".")

from engine.shopper_v2.state import MAX_FOCUS_STACK_DEPTH, Topic  # noqa: E402
from engine.shopper_v2.topic_transition import (  # noqa: E402
    TransitionResult,
    apply_transition,
    decide_transition,
)

_checks = []


def check(label: str, ok: bool) -> None:
    _checks.append((label, ok))
    print(("  ok      " if ok else "  FAIL    ") + label)


def topic(topic_id, kind, label, state="active", product_ids=None, minutes_ago=0):
    return Topic(
        topic_id=topic_id, kind=kind, label=label, state=state,
        product_ids=product_ids or [],
        last_active_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
    )


def main() -> bool:
    # Rule 5: empty stack, nothing new -> continue, no active topic.
    r = decide_transition(focus_stack=[], message="hi", delta_goal=None, has_new_shopping_content=False)
    check("empty stack + no signal -> continue, topic_id=None", r.action == "continue" and r.topic_id is None)
    check("empty stack -> mission_call never allowed", not r.mission_call_allowed)

    # Rule 5: active discovery topic, no new content -> continue.
    stack = [topic("t1", "discovery", "running shoes")]
    r = decide_transition(focus_stack=stack, message="what colors do you have", delta_goal=None, has_new_shopping_content=False)
    check("active discovery, no new content -> continue t1", r.action == "continue" and r.topic_id == "t1")
    check("continue on non-mission topic -> mission call not allowed", not r.mission_call_allowed)

    # Rule 5: active MISSION topic, no new goal -> continue, mission call ALWAYS allowed.
    stack = [topic("m1", "mission", "shoes and socks")]
    r = decide_transition(focus_stack=stack, message="also add a hydration belt", delta_goal=None, has_new_shopping_content=True)
    check("active mission topic -> continue, mission call unconditionally allowed", r.action == "continue" and r.mission_call_allowed)

    # Rule 4: PUSH - new unrelated goal, no paused topics, no mission anywhere.
    stack = [topic("t1", "discovery", "running shoes")]
    r = decide_transition(focus_stack=stack, message="show me coffee", delta_goal="coffee", has_new_shopping_content=True)
    check("new unrelated goal, no mission exists -> push", r.action == "push")
    check("push never allows mission call", not r.mission_call_allowed)

    # Rule 4: goal restates the SAME active topic's label -> continue, not push.
    stack = [topic("t1", "discovery", "running shoes")]
    r = decide_transition(focus_stack=stack, message="I want running shoes under 5000", delta_goal="running shoes", has_new_shopping_content=True)
    check("goal restates active topic's own label -> continue (no spurious push)", r.action == "continue" and r.topic_id == "t1")

    # Rule 1/2: RESUME - message names a paused topic by label, unambiguously.
    stack = [
        topic("t1", "discovery", "running shoes", state="paused", minutes_ago=5),
        topic("t2", "discovery", "coffee", state="active"),
    ]
    r = decide_transition(focus_stack=stack, message="actually go back to the shoes", delta_goal=None, has_new_shopping_content=False)
    check("message names a paused topic's label -> resume it", r.action == "resume" and r.topic_id == "t1")

    # Rule 1/2: RESUME via historical-reference phrasing when only one paused topic exists.
    stack = [
        topic("t1", "discovery", "socks", state="paused", minutes_ago=5),
        topic("t2", "discovery", "coffee", state="active"),
    ]
    r = decide_transition(focus_stack=stack, message="what was the one you showed me earlier", delta_goal=None, has_new_shopping_content=False)
    check("historical phrasing, exactly one paused topic -> resume it", r.action == "resume" and r.topic_id == "t1")

    # Rule 1/2: historical-reference phrasing with TWO paused topics -> most recently paused wins.
    stack = [
        topic("t1", "discovery", "socks", state="paused", minutes_ago=10),
        topic("t2", "discovery", "hats", state="paused", minutes_ago=2),
        topic("t3", "discovery", "coffee", state="active"),
    ]
    r = decide_transition(focus_stack=stack, message="what did you show me before", delta_goal=None, has_new_shopping_content=False)
    check("historical phrasing, two paused topics -> most-recently-paused wins", r.action == "resume" and r.topic_id == "t2")

    # Rule 3: AMBIGUOUS_MISSION_CHECK - mission exists paused, new unrelated-looking
    # content doesn't match active topic -> must ask interpret_mission_items.
    stack = [
        topic("m1", "mission", "shoes and socks", state="paused", minutes_ago=3),
        topic("t2", "discovery", "coffee", state="active"),
    ]
    r = decide_transition(focus_stack=stack, message="also get me a water bottle", delta_goal="water bottle", has_new_shopping_content=True)
    check("mission paused + new unrelated-looking content -> ambiguous_mission_check", r.action == "ambiguous_mission_check" and r.topic_id == "m1")
    check("ambiguous check allows the mission call", r.mission_call_allowed)

    # Rule 3 must NOT fire when the new content clearly matches the ACTIVE topic instead.
    stack = [
        topic("m1", "mission", "shoes and socks", state="paused", minutes_ago=3),
        topic("t2", "discovery", "coffee", state="active"),
    ]
    r = decide_transition(focus_stack=stack, message="I want a different coffee blend", delta_goal="coffee blend", has_new_shopping_content=True)
    check("new content matches ACTIVE topic -> no ambiguity, continue", r.action == "continue" and r.topic_id == "t2")

    # Proof: no mission anywhere -> ambiguous_mission_check structurally impossible.
    stack = [topic("t1", "discovery", "running shoes", state="active")]
    r = decide_transition(focus_stack=stack, message="show me coffee", delta_goal="coffee", has_new_shopping_content=True)
    check("no mission topic anywhere -> ambiguous_mission_check never returned", r.action != "ambiguous_mission_check")

    # Proof: mission ALREADY active -> never ambiguous, always continue+allowed (rule 5 wins).
    stack = [topic("m1", "mission", "shoes and socks", state="active")]
    r = decide_transition(focus_stack=stack, message="also get me a water bottle", delta_goal="water bottle", has_new_shopping_content=True)
    check("mission already active -> continue (not ambiguous), mission call allowed", r.action == "continue" and r.mission_call_allowed)

    # Explicit commerce turn: this module is never even consulted for cart-resolved
    # turns per the routing contract - documented here as a design note, not testable
    # in isolation (graph.py's own wiring enforces it in Step 5).

    # --- apply_transition ---
    t1 = topic("t1", "discovery", "shoes", state="active")
    stack = apply_transition(
        focus_stack=[t1], decision=TransitionResult("push", None, False),
        new_topic_id="t2", new_topic_kind="discovery", new_topic_label="coffee",
        new_topic_product_ids=["P1"],
    )
    shapes = {t.topic_id: t.state for t in stack}
    check("push: old active demoted, new topic active", shapes == {"t1": "paused", "t2": "active"})

    stack2 = apply_transition(focus_stack=stack, decision=TransitionResult("resume", "t1", False))
    shapes2 = {t.topic_id: t.state for t in stack2}
    check("resume: named topic reactivated, prior active demoted", shapes2 == {"t1": "active", "t2": "paused"})

    big_stack = [t1]
    for i in range(MAX_FOCUS_STACK_DEPTH + 3):
        big_stack = apply_transition(
            focus_stack=big_stack, decision=TransitionResult("push", None, False),
            new_topic_id=f"x{i}", new_topic_kind="discovery", new_topic_label=f"topic{i}",
        )
    paused_count = sum(1 for t in big_stack if t.state == "paused")
    check("depth cap holds at MAX_FOCUS_STACK_DEPTH", paused_count == MAX_FOCUS_STACK_DEPTH)
    check("oldest paused topic evicted, not merely truncated arbitrarily", "t1" not in [t.topic_id for t in big_stack])

    try:
        apply_transition(focus_stack=[], decision=TransitionResult("ambiguous_mission_check", "m1", True))
        check("apply_transition rejects ambiguous_mission_check (caller must resolve first)", False)
    except ValueError as exc:
        check("apply_transition rejects ambiguous_mission_check (caller must resolve first), saying why", "ambiguous" in str(exc).lower())

    return all(ok for _, ok in _checks)


if __name__ == "__main__":
    ok = main()
    print()
    print("all topic-transition checks passed" if ok else "SOME CHECKS FAILED")
    sys.exit(0 if ok else 1)
