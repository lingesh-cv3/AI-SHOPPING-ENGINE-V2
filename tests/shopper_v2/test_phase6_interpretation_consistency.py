"""Deterministic regression matrix for the two interpretation-quality fixes:

1. Natural-language cart-operation resolution (remove/clear/set_quantity
   must reach the single authoritative interpretation call's own cart
   resolution, including references to real CART LINE products, never a
   second, separate LLM classifier).
2. Compound/follow-up interpretation consistency (a correction/cancellation/
   pause tag on a claim must never silently discard a real, separately-named
   operation the SAME claim's own `capability` field already resolved).

Model-free throughout - constructs claims/contexts by hand, no network, no
LLM key required.

    python tests/shopper_v2/test_phase6_interpretation_consistency.py
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


async def capability_override_matrix() -> None:
    import engine.shopper_v2.graph as graph_mod
    from engine.shopper_v2.capability import Capability
    from engine.shopper_v2.tracing import TurnTrace

    class FakeVerified:
        def __init__(self, *, capability, question_type="retrieve", repair="none"):
            self.capability = capability
            self.question_type = question_type
            self.conversational_repair = repair
            self.secondary_operation = None
            self.durable_fact_candidates = []
            self.resolved_reference = None
            self.resolved_secondary = None
            self.needs_clarification = False
            self.cart_operation = "none"
            self.catalog_categories = []

    async def route_with(verified) -> str:
        state = {
            "friction": None,
            "hinted_action_type": None,
            "message_intent": None,
            "verified_interpretation": verified,
            "trace": TurnTrace(),
            "message": "irrelevant for this branch",
            "new_state": None,
            "adapter": None,
        }
        out = await graph_mod.node_route_capability(state)
        return out.get("capability")

    # 1. A genuine compound turn: correction + a real search. Must NOT
    #    collapse to ANSWER_QUESTION - this is the exact live-observed bug.
    cap = await route_with(FakeVerified(capability="discover", repair="correction"))
    check("correction + real 'discover' capability is respected, not overridden", cap == Capability.DISCOVER)

    # 2. Same shape for cart_action.
    cap = await route_with(FakeVerified(capability="cart_action", repair="correction"))
    check("correction + real 'cart_action' capability is respected", cap == Capability.CART_ACTION)

    # 3. 'mission' is a valid model capability with no `Capability` enum
    #    member of its own (routed via a separate graph edge) - a
    #    cancellation tag must still not steamroll it into ANSWER_QUESTION.
    cap = await route_with(FakeVerified(capability="mission", repair="cancellation"))
    check("cancellation + real 'mission' capability is not steamrolled into answer_question", cap != Capability.ANSWER_QUESTION)

    # 4. The ORIGINAL case this branch exists for: a bare cancellation with
    #    nothing else real to do (capability='conversational') still routes
    #    to ANSWER_QUESTION/state_change - not a regression.
    cap = await route_with(FakeVerified(capability="conversational", repair="cancellation"))
    check("a bare cancellation with no real operation still routes to answer_question", cap == Capability.ANSWER_QUESTION)

    # 5. question_type='state_change' alone (no repair) still forces it too.
    cap = await route_with(FakeVerified(capability="discover", question_type="state_change"))
    check("question_type='state_change' alone still forces answer_question", cap == Capability.ANSWER_QUESTION)

    # 6. capability='unknown' + pause -> still answer_question (nothing real named).
    cap = await route_with(FakeVerified(capability="unknown", repair="pause"))
    check("pause + no real capability still routes to answer_question", cap == Capability.ANSWER_QUESTION)


async def cart_scope_matrix() -> None:
    from engine.shopper_v2.verification import (
        VerifiedInterpretation, _candidates_for_scope, _cross_check_scope, resolve_reference,
    )
    from engine.shopper_v2.conversation_context import ConversationContext, CartSnapshot, CartLineSnapshot, Topic
    from engine.reasoning.service import InterpretationResult
    from shared.models.commerce import Money, Product

    trailblazer = Product(product_id="P1001", title="Trailblazer Running Shoe", price=Money(amount=4299, currency="INR"), availability="IN_STOCK", description="lightweight trainer")
    socks = Product(product_id="P4001", title="Performance Socks Three Pack", price=Money(amount=599, currency="INR"), availability="IN_STOCK", description="running socks")

    # Cart has the Trailblazer; nothing is currently "active" (shopper
    # searched something else earlier, unrelated).
    context = ConversationContext(
        session_id="s1", connection_id="conn_demo",
        active_topic=Topic(topic_id="t1", kind="discovery", label="socks", product_ids=["P4001"]),
        cart_snapshot=CartSnapshot(cart_id="c1", lines=[CartLineSnapshot(product_id="P1001", title="Trailblazer Running Shoe", quantity=1)]),
    )
    products = [trailblazer, socks]

    def claim(**kw):
        base = dict(
            capability="cart_action", context_action="continue", reference_kind="named",
            reference_scope="cart", cart_operation="remove", reference_names=["Trailblazer"],
        )
        base.update(kw)
        return InterpretationResult(**base)

    # "remove the Trailblazer" -> scope='cart', kind='named' -> resolves
    # against the REAL cart line, even though it's not the active topic.
    resolved = resolve_reference(claim(), context, products, "remove the Trailblazer from my cart")
    check("named cart reference resolves against a real cart line, not the active topic", resolved.product_ids == ["P1001"])

    # "clear my cart" -> kind='all', scope='cart' -> resolves to every real cart line.
    resolved_all = resolve_reference(claim(reference_kind="all", reference_names=[]), context, products, "clear my cart")
    check("an 'all'-kind cart reference resolves to every real cart line", resolved_all.product_ids == ["P1001"])

    # Ambiguous: two products in cart, message doesn't disambiguate by name
    # (kind='named' with a name that doesn't match either) -> resolve_reference
    # only ever narrows to real candidates; an unmatched name should not
    # silently pick something - covered by resolved.candidates_considered
    # being the full cart set and product_ids empty when nothing named matches.
    two_line_context = ConversationContext(
        session_id="s1", connection_id="conn_demo",
        cart_snapshot=CartSnapshot(cart_id="c1", lines=[
            CartLineSnapshot(product_id="P1001", title="Trailblazer Running Shoe", quantity=1),
            CartLineSnapshot(product_id="P4001", title="Performance Socks Three Pack", quantity=1),
        ]),
    )
    resolved_ambiguous = resolve_reference(
        claim(reference_names=["something else entirely"]), two_line_context, products,
        "remove something else entirely",
    )
    check("an unmatched named reference never silently picks a cart line", not resolved_ambiguous.product_ids)
    check("an unmatched named reference still reports real cart candidates to ask about", len(resolved_ambiguous.candidates_considered) == 2)


async def main() -> None:
    print("Capability-override matrix (compound-turn consistency)")
    await capability_override_matrix()
    print()
    print("Cart-scope reference resolution matrix (natural-language cart operations)")
    await cart_scope_matrix()
    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all interpretation-consistency checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
