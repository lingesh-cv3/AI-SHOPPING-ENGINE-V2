"""State-resolution fallback in `recent_relevant_product_ids`
(`engine/shopper_v2/state.py`).

Live-reproduced gap this closes (2026-09), found via forensic HTTP-path
logging, not guesswork: "Show me some running shoes" (a plain discovery
turn that never pushes a real Focus Stack topic - `delta.goal` is
unreliable for this phrasing) followed by "I'm looking for something
lightweight" (which DOES get a `delta.goal`, so `topic_transition.py`
correctly pushes a brand-new topic, shape-only, `product_ids=[]` until a
LATER discovery/mission node attaches real ones) reached
`tradeoff.py::run_tradeoff_turn` with a Focus Stack that was "populated"
(one active topic) but genuinely empty - `recent_relevant_product_ids`'s
own "Focus Stack is the SOLE source once populated" rule then refused to
look at `active_products`/`last_rendered`, which still honestly held the
real, already-verified ids for what was literally on screen. Result: "I
don't have anything currently in view to re-rank against that yet." - a
false negative, not a classification failure (question_type was already
correctly `attribute_refinement` on that exact turn).

Model-free: no `ReasoningService`/LLM call anywhere in this file - this is
a pure state-resolution fix, not a prompt/classifier change (per the
approved fix scope, no interpretation/prompts/QuestionType code is touched
by it).

    python tests/shopper_v2/test_phase18_state_resolution.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


def _p(pid: str, title: str, price: str, description: str):
    from shared.models import Availability, Money, Product
    return Product(
        product_id=pid, title=title, description=description,
        price=Money(amount=price, currency="INR"),
        availability=Availability.IN_STOCK,
    )


_SHOES = [
    _p("P1001", "Trailblazer Running Shoe", "4299.00", "Lightweight daily trainer with a cushioned midsole."),
    _p("P1002", "Marathon Pro Racing Shoe", "12499.00", "Carbon plate, built for race day."),
    _p("P1003", "Everyday Canvas Sneaker", "1899.00", "Simple canvas upper, rubber sole."),
]


def _fresh_state():
    from engine.shopper_v2.state import ShoppingStateV2
    return ShoppingStateV2(session_id="s1", connection_id="conn_demo")


def _topic(topic_id: str, state: str, product_ids: list[str]):
    from engine.shopper_v2.state import Topic
    return Topic(topic_id=topic_id, kind="discovery", label="running shoes", product_ids=product_ids, state=state)


def test_focus_stack_with_real_ids_wins():
    """Case 1: Focus Stack has valid product ids -> use Focus Stack,
    unchanged from before this fix."""
    from engine.shopper_v2.state import recent_relevant_product_ids

    state = _fresh_state()
    state.focus_stack = [_topic("t1", "active", ["P1001", "P1002"])]
    state.active_products = ["P9999"]  # deliberately different - must NOT be used
    ids = recent_relevant_product_ids(state, minimum=1)
    check(
        "Focus Stack with real ids is used as-is, legacy field ignored",
        ids == ["P1001", "P1002"],
    )


def test_empty_active_topic_falls_back_to_active_products():
    """Case 2a: Focus Stack exists but its active topic resolved to zero
    ids (the live-reproduced shape-only-push case) -> fall back to
    `active_products`."""
    from engine.shopper_v2.state import recent_relevant_product_ids

    state = _fresh_state()
    state.focus_stack = [_topic("t1", "active", [])]
    state.active_products = ["P1001", "P1002", "P1003"]
    ids = recent_relevant_product_ids(state, minimum=1)
    check(
        "empty active topic falls back to active_products, never returns empty",
        ids == ["P1001", "P1002", "P1003"],
    )


def test_empty_active_topic_falls_back_to_last_rendered():
    """Case 2b: same shape, but `active_products` is ALSO empty - the
    fallback must still reach `last_rendered` before giving up."""
    from engine.shopper_v2.state import LastRendered, recent_relevant_product_ids

    state = _fresh_state()
    state.focus_stack = [_topic("t1", "active", [])]
    state.active_products = []
    state.last_rendered = LastRendered(product_ids=["P1001", "P1003"])
    ids = recent_relevant_product_ids(state, minimum=1)
    check(
        "empty active topic + empty active_products falls back to last_rendered",
        ids == ["P1001", "P1003"],
    )


def test_no_real_context_anywhere_stays_honest():
    """Case 3: Focus Stack empty, `active_products` empty, no
    `last_rendered` - must degrade honestly (empty list), never invent
    candidates."""
    from engine.shopper_v2.state import recent_relevant_product_ids

    state = _fresh_state()
    state.focus_stack = [_topic("t1", "active", [])]
    state.active_products = []
    state.last_rendered = None
    ids = recent_relevant_product_ids(state, minimum=1)
    check("no real context anywhere returns empty, never invents", ids == [])


def test_paused_topics_still_checked_before_fallback():
    """Focus Stack's own paused-topic lookback must still be tried BEFORE
    falling back to the legacy fields - the fallback is a last resort, not
    a shortcut around real Focus Stack data."""
    from engine.shopper_v2.state import recent_relevant_product_ids

    state = _fresh_state()
    state.focus_stack = [
        _topic("t1", "active", []),
        _topic("t0", "paused", ["P1002"]),
    ]
    state.active_products = ["P9999"]  # must NOT be used - paused topic already satisfies minimum
    ids = recent_relevant_product_ids(state, minimum=1)
    check("a real paused topic is used before falling back to legacy fields", ids == ["P1002"])


def test_live_reproduced_sequence_end_to_end():
    """The exact live-reproduced sequence: "Show me some running shoes"
    (never pushes a Focus Stack topic) then "I'm looking for something
    lightweight" (pushes a shape-only topic with empty product_ids) - the
    re-rank must use the real, already-shown shoes, never the honest
    "nothing in view" decline."""
    from engine.shopper_v2.tradeoff import run_tradeoff_turn

    class _StubAdapter:
        async def get_product(self, product_id: str):
            return {p.product_id: p for p in _SHOES}[product_id]

    state = _fresh_state()
    # Turn 1's real, already-verified result: legacy fields populated
    # (as node_finalize always does), Focus Stack still untouched.
    state.active_products = [p.product_id for p in _SHOES]
    state.recent_product_groups = [[p.product_id for p in _SHOES]]
    from engine.shopper_v2.state import LastRendered
    state.last_rendered = LastRendered(product_ids=[p.product_id for p in _SHOES])
    # Turn 2's topic_transition push: shape only, no content yet - this is
    # the exact state `node_topic_transition` leaves behind for a PUSH
    # decision, per its own docstring.
    state.focus_stack = [_topic("t1", "active", [])]
    state.priorities = ["lightweight"]

    reply, evidence, products, new_active_ids = asyncio.run(
        run_tradeoff_turn(adapter=_StubAdapter(), message="I'm looking for something lightweight", state=state)
    )
    check(
        "re-rank finds the real, already-shown shoes instead of declining",
        bool(products) and "nothing currently in view" not in reply,
    )
    check(
        "the lightweight trainer is grounded as the top pick, not invented",
        bool(products) and products[0].product_id == "P1001",
    )


def main() -> None:
    test_focus_stack_with_real_ids_wins()
    test_empty_active_topic_falls_back_to_active_products()
    test_empty_active_topic_falls_back_to_last_rendered()
    test_no_real_context_anywhere_stays_honest()
    test_paused_topics_still_checked_before_fallback()
    test_live_reproduced_sequence_end_to_end()
    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
        raise SystemExit(1)
    print("  all ok")


if __name__ == "__main__":
    main()
