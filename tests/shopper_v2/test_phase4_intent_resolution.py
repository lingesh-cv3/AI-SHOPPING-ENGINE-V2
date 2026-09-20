"""General cart-intent resolution (`engine/shopper_v2/intent_resolution.py`)
- the non-phrase-specific interpretation layer added after the Phase 4
report identified the model's general-purpose `propose_actions` tool does
not reliably classify natural cart-intent language.

Model-free: uses a stub `ReasoningService`-shaped object whose
`interpret_cart_reference` returns a scripted `CartReferenceIntent` per
case, so these are deterministic, repeatable checks of the RESOLUTION
logic (ordinal/pronoun/name -> real product id, or an honest clarifying
question) - not of whether a live model classifies correctly, which is a
separate, already-documented characteristic (see PROGRESS.md).

    python tests/shopper_v2/test_phase4_intent_resolution.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from shared.models import Availability, Money, Product, ProductSearchResult  # noqa: E402

from engine.reasoning.service import CartReferenceIntent  # noqa: E402
from engine.shopper_v2.intent_resolution import resolve_cart_target  # noqa: E402
from engine.shopper_v2.state import ShoppingStateV2  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


def _p(pid: str, title: str) -> Product:
    return Product(
        product_id=pid, title=title,
        price=Money(amount="1000.00", currency="INR"),
        availability=Availability.IN_STOCK,
    )


CATALOG = {
    "P1001": _p("P1001", "Trailblazer Running Shoe"),
    "P1005": _p("P1005", "Fell Runner Trail Shoe"),
}


class _StubAdapter:
    def __init__(self, catalog=None, search_hits=None):
        self._catalog = catalog or CATALOG
        self._search_hits = search_hits or []

    async def get_product(self, product_id: str) -> Product:
        return self._catalog[product_id]

    async def search_products(self, query: str, *, limit: int = 20):
        return ProductSearchResult(query=query, products=self._search_hits)


class _ScriptedReasoning:
    """`enabled=True` (real `ReasoningService` also exposes this), and
    `interpret_cart_reference` returns whatever `CartReferenceIntent` this
    test scripted, without a real model call."""

    enabled = True

    def __init__(self, intent: CartReferenceIntent | None):
        self._intent = intent

    async def interpret_cart_reference(self, **kwargs):
        return self._intent


def _state(active_products: list[str]) -> ShoppingStateV2:
    return ShoppingStateV2(session_id="s", connection_id="c", active_products=active_products)


async def no_active_products_never_becomes_cart_intent() -> None:
    """Requirement 9: a plain discovery turn (nothing shown yet) can never
    be interpreted as cart intent, regardless of what the interpreter
    might have said - `resolve_cart_target` short-circuits before ever
    calling it."""
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(is_cart_intent=True, reference_type="pronoun")
    )
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="I need running shoes",
        history=None, state=_state([]),
    )
    check("no active_products -> never cart intent, even if scripted to say yes", not result.is_cart_intent)


async def ordinal_reference_resolves_to_the_real_position() -> None:
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(is_cart_intent=True, reference_type="ordinal", position=2)
    )
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="add the second one",
        history=None, state=_state(["P1001", "P1005"]),
    )
    check("ordinal position 2 resolves to the real second product", result.product_id == "P1005")
    check("cart intent detected", result.is_cart_intent)


async def ordinal_out_of_range_asks_instead_of_guessing() -> None:
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(is_cart_intent=True, reference_type="ordinal", position=5)
    )
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="add the fifth one",
        history=None, state=_state(["P1001", "P1005"]),
    )
    check("out-of-range ordinal never guesses a product", result.product_id is None)
    check("an honest clarifying question is returned instead", bool(result.clarifying_question))


async def pronoun_resolves_only_when_unambiguous() -> None:
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(is_cart_intent=True, reference_type="pronoun")
    )
    single = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="add it",
        history=None, state=_state(["P1001"]),
    )
    check("a bare pronoun with exactly one shown product resolves to it", single.product_id == "P1001")

    ambiguous = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="add it",
        history=None, state=_state(["P1001", "P1005"]),
    )
    check("a bare pronoun with two shown products is NOT resolved (ambiguous)", ambiguous.product_id is None)
    check("ambiguous pronoun still asks which one", bool(ambiguous.clarifying_question))


async def name_reference_resolves_against_shown_products() -> None:
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(
            is_cart_intent=True, reference_type="name", named_hint="Trailblazer"
        )
    )
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="get me the Trailblazer",
        history=None, state=_state(["P1001", "P1005"]),
    )
    check("a name reference resolves to the matching shown product", result.product_id == "P1001")


async def name_reference_falls_back_to_a_live_verified_search() -> None:
    """'get me the X' for something not yet shown this turn - only
    accepted if a live, bounded search finds exactly one confident match,
    never invented."""
    water_bottle = _p("P3001", "Insulated Water Bottle 750ml")
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(
            is_cart_intent=True, reference_type="name", named_hint="water bottle"
        )
    )
    adapter = _StubAdapter(search_hits=[water_bottle])
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=adapter, message="get me the water bottle",
        history=None, state=_state(["P1001"]),  # shown product is unrelated
    )
    check("an unshown named product is resolved via a live, verified search", result.product_id == "P3001")


async def not_cart_intent_falls_through_to_discovery() -> None:
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(is_cart_intent=False, reference_type="none")
    )
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="do you have anything lighter?",
        history=None, state=_state(["P1001"]),
    )
    check("a genuine follow-up question is never treated as cart intent", not result.is_cart_intent)
    check("no product resolved for a non-cart-intent turn", result.product_id is None)


async def interpreter_unavailable_degrades_to_no_cart_intent() -> None:
    reasoning = _ScriptedReasoning(None)  # simulates LLM unavailable/timeout
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="add that",
        history=None, state=_state(["P1001"]),
    )
    check("interpreter unavailable -> honest 'not cart intent', never a guess", not result.is_cart_intent)


async def reasoning_disabled_never_calls_the_interpreter() -> None:
    class _Disabled:
        enabled = False

        async def interpret_cart_reference(self, **kwargs):
            raise AssertionError("must not be called when reasoning is disabled")

    result = await resolve_cart_target(
        reasoning=_Disabled(), adapter=_StubAdapter(), message="add that",
        history=None, state=_state(["P1001"]),
    )
    check("a disabled reasoning service short-circuits cleanly", not result.is_cart_intent)


def main() -> int:
    asyncio.run(no_active_products_never_becomes_cart_intent())
    asyncio.run(ordinal_reference_resolves_to_the_real_position())
    asyncio.run(ordinal_out_of_range_asks_instead_of_guessing())
    asyncio.run(pronoun_resolves_only_when_unambiguous())
    asyncio.run(name_reference_resolves_against_shown_products())
    asyncio.run(name_reference_falls_back_to_a_live_verified_search())
    asyncio.run(not_cart_intent_falls_through_to_discovery())
    asyncio.run(interpreter_unavailable_degrades_to_no_cart_intent())
    asyncio.run(reasoning_disabled_never_calls_the_interpreter())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all general cart-intent resolution checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
