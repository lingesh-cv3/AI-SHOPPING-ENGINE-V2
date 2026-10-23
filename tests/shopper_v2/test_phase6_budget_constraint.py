"""Regression tests for the budget-constraint-loss bug found live:

"Show me running shoes under 5000 and compare the best two" compared
Featherline Speed Running Shoe at INR 6299 - over the stated budget.

Root cause: `budget_amount` in `ShoppingStateV2` came ONLY from the model's
own `shopping_state.budget_amount` field on the SAME interpretation call
that also had to resolve a compound search + comparison - a live-observed
compound turn dropped it, and downstream (both the primary discovery hard
filter and the secondary comparison) had nothing to filter against.

Two fixes, both covered here:
1. `understanding.py::_extract_budget_amount` - a deterministic backstop
   that fills the gap ONLY when the model's own field came back empty,
   never overriding a real model value.
2. `graph.py::_execute_secondary_operation`'s 'compare' branch - an
   explicit hard filter against `new_state.budget_amount`, since the
   named-reference `retrieve()` widening call has no budget awareness of
   its own.

Model-free throughout except where noted - no network, no LLM key
required.

    python tests/shopper_v2/test_phase6_budget_constraint.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


def extractor_matrix() -> None:
    from engine.shopper_v2.understanding import _extract_budget_amount

    cases = [
        ("Show me running shoes under 5000 and compare the best two", "5000"),
        ("waterproof trail shoes under 10000", "10000"),
        ("something below 2000", "2000"),
        ("less than 3000 rupees", "3000"),
        ("up to 4500", "4500"),
        ("max 6000", "6000"),
        ("no more than 1500", "1500"),
        ("size 8", None),
        ("add the Trailblazer to my cart", None),
        ("I need running shoes for a race next month", None),
        # Direct budget STATEMENT phrasing (2026-09, live-reproduced gap):
        # no "under"/"below"/etc. constraint word at all - the whole
        # message IS the budget statement.
        ("My budget is 5000", "5000"),
        ("budget of 4500", "4500"),
        ("my budget is around 3000", "3000"),
        ("I want to spend 2000", "2000"),
        ("spending around 6000", "6000"),
        ("budget shoes, size 8", None),
        ("I have a budget for shoes", None),
    ]
    for message, expected in cases:
        got = _extract_budget_amount(message)
        check(f"extract({message!r}) == {expected!r}", got == expected)


async def understand_backstop() -> None:
    from engine.reasoning.service import ReasoningService
    from engine.shopper_v2.understanding import understand
    from engine.shopper_v2.conversation_context import ConversationContext

    class FakeToolResult:
        def __init__(self, args):
            self.tool_calls = [{"name": "interpret_conversation", "arguments": args}]
            self.model = "fake-model"

    context = ConversationContext(session_id="s1", connection_id="conn_demo")

    # Case 1: model's own shopping_state omits budget_amount entirely -
    # the message plainly states one - backstop must fill it.
    r = ReasoningService(client=None)
    r._client = AsyncMock()
    r._enabled = True
    r._client.complete = AsyncMock(return_value=FakeToolResult({
        "capability": "discover", "context_action": "push", "rationale": "x",
        "shopping_state": {"goal": "running shoes"},
    }))
    result = await understand(
        reasoning=r, message="Show me running shoes under 5000 and compare the best two",
        history=None, shopping_state={}, memory_facts=None, context=context,
    )
    check("model omits budget_amount, message states one -> backstop fills it", result.delta.budget_amount == "5000")

    # Case 2: model DOES supply a real budget_amount - backstop must never
    # override it, even if the message's own number differs (never trust
    # the deterministic pattern over a real model value).
    r2 = ReasoningService(client=None)
    r2._client = AsyncMock()
    r2._enabled = True
    r2._client.complete = AsyncMock(return_value=FakeToolResult({
        "capability": "discover", "context_action": "push", "rationale": "x",
        "shopping_state": {"budget_amount": 4200},
    }))
    result2 = await understand(
        reasoning=r2, message="Show me running shoes under 5000 and compare the best two",
        history=None, shopping_state={}, memory_facts=None, context=context,
    )
    check("model's own real budget_amount is never overridden by the backstop", result2.delta.budget_amount == "4200")

    # Case 3: no budget language at all, model omits it too -> stays None,
    # never invented.
    r3 = ReasoningService(client=None)
    r3._client = AsyncMock()
    r3._enabled = True
    r3._client.complete = AsyncMock(return_value=FakeToolResult({
        "capability": "discover", "context_action": "push", "rationale": "x",
        "shopping_state": {"goal": "running shoes"},
    }))
    result3 = await understand(
        reasoning=r3, message="Show me running shoes",
        history=None, shopping_state={}, memory_facts=None, context=context,
    )
    check("no budget language anywhere -> stays None, never invented", result3.delta.budget_amount is None)


async def secondary_compare_budget_filter() -> None:
    import engine.shopper_v2.graph as graph_mod
    from engine.reasoning.service import SecondaryOperation, Synthesis
    from engine.shopper_v2.retrieval import RetrievalResult
    from engine.shopper_v2.state import ShoppingStateV2
    from shared.models.commerce import Money, Product

    featherline = graph_mod  # placeholder to keep import ordering explicit
    trailblazer = Product(product_id="P1001", title="Trailblazer Running Shoe", price=Money(amount=4299, currency="INR"), availability="IN_STOCK", description="everyday trainer")
    featherline_p = Product(product_id="P1004", title="Featherline Speed Running Shoe", price=Money(amount=6299, currency="INR"), availability="IN_STOCK", description="racing shoe")
    starter = Product(product_id="P1002", title="Starter Road Running Shoe", price=Money(amount=1999, currency="INR"), availability="IN_STOCK", description="budget road shoe")

    async def fake_retrieve(adapter, query, *, limit=5):
        # Simulates the named-reference widening call finding the
        # EXPLICITLY NAMED product for real - Featherline is a genuine
        # catalog match for its own name, exactly as a live named
        # reference resolves.
        if "featherline" in query.lower():
            return RetrievalResult(products=[featherline_p], unavailable=False)
        return RetrievalResult(products=[], unavailable=False)

    graph_mod.retrieve = fake_retrieve

    fake_reasoning = AsyncMock()
    fake_reasoning.synthesize = AsyncMock(return_value=Synthesis(
        text="Featherline Speed Running Shoe is the superior pick for racing.",
        cited_reason="racing shoe", used_model=True,
    ))

    # This is exactly the live-observed shape: three primary products, one
    # (Featherline) over the stated 5000 budget, secondary_operation asks
    # to compare "the best two" (no named reference - relies purely on
    # `primary_products` + the budget filter).
    state = ShoppingStateV2(session_id="s1", connection_id="conn_demo", budget_amount="5000")
    secondary_op = SecondaryOperation(capability="compare", reference_names=[])

    result_text = await graph_mod._execute_secondary_operation(
        secondary_op=secondary_op,
        primary_products=[featherline_p, trailblazer, starter],
        adapter=object(), reasoning=fake_reasoning,
        message="Show me running shoes under 5000 and compare the best two",
        new_state=state,
    )
    check("secondary comparison never names the over-budget product", result_text is not None and "6299" not in result_text and "Featherline" not in result_text)
    check("secondary comparison names real, in-budget products instead", result_text is not None and "Trailblazer" in result_text and "Starter Road" in result_text)

    # Multi-turn case: budget was stated on an EARLIER turn (already
    # persisted on `new_state`, not this turn's own delta) - the secondary
    # comparison must still respect it identically.
    state_persisted_budget = ShoppingStateV2(session_id="s1", connection_id="conn_demo", budget_amount="5000")
    result_text2 = await graph_mod._execute_secondary_operation(
        secondary_op=secondary_op,
        primary_products=[featherline_p, trailblazer, starter],
        adapter=object(), reasoning=fake_reasoning,
        message="compare the best two",
        new_state=state_persisted_budget,
    )
    check("a budget stated on an earlier turn still filters a later compound turn's comparison", result_text2 is not None and "Featherline" not in result_text2)

    # Explicit named-reference case (2026-09, live-reproduced): the shopper
    # NAMES the over-budget product directly ("compare Featherline with the
    # Trailblazer") - it must not enter the comparison merely because it
    # was explicitly named. The named-reference `retrieve()` widening call
    # genuinely finds it (a real catalog match for its own name); the
    # budget filter must still exclude it.
    state_named_over_budget = ShoppingStateV2(session_id="s1", connection_id="conn_demo", budget_amount="5000")
    secondary_op_named = SecondaryOperation(capability="compare", reference_names=["Featherline"])
    result_text4 = await graph_mod._execute_secondary_operation(
        secondary_op=secondary_op_named,
        primary_products=[trailblazer],
        adapter=object(), reasoning=fake_reasoning,
        message="Compare the Featherline Speed Running Shoe with the Trailblazer",
        new_state=state_named_over_budget,
    )
    check(
        "an explicitly named over-budget product cannot enter the comparison",
        result_text4 is None or "Featherline" not in result_text4,
    )

    # Same named-over-budget case, but the budget was established on an
    # EARLIER turn via a direct statement ("My budget is 5000") rather than
    # a constraint phrase in THIS turn's own message.
    state_named_multi_turn = ShoppingStateV2(session_id="s1", connection_id="conn_demo", budget_amount="5000")
    result_text5 = await graph_mod._execute_secondary_operation(
        secondary_op=secondary_op_named,
        primary_products=[trailblazer],
        adapter=object(), reasoning=fake_reasoning,
        message="Compare the Featherline Speed Running Shoe with the Trailblazer",
        new_state=state_named_multi_turn,
    )
    check(
        "multi-turn: a budget stated earlier still excludes an explicitly named over-budget product later",
        result_text5 is None or "Featherline" not in result_text5,
    )

    # Control: no budget at all -> Featherline is a legitimate candidate.
    state_no_budget = ShoppingStateV2(session_id="s1", connection_id="conn_demo")
    result_text3 = await graph_mod._execute_secondary_operation(
        secondary_op=secondary_op,
        primary_products=[featherline_p, trailblazer],
        adapter=object(), reasoning=fake_reasoning,
        message="compare these two",
        new_state=state_no_budget,
    )
    check("with no stated budget, a real product is not wrongly excluded", result_text3 is not None and "Featherline" in result_text3)


async def main() -> None:
    print("Deterministic budget-extraction matrix")
    extractor_matrix()
    print()
    print("understand() backstop (model-omitted vs. model-supplied budget)")
    await understand_backstop()
    print()
    print("Secondary comparison budget hard-filter (the exact live-reported case)")
    await secondary_compare_budget_filter()
    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all budget-constraint checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
