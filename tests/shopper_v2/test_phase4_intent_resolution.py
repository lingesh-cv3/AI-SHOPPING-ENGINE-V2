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

from shared.models import Availability, Cart, CartLine, Money, Product, ProductSearchResult  # noqa: E402

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
    def __init__(self, catalog=None, search_hits=None, cart=None):
        self._catalog = catalog or CATALOG
        self._search_hits = search_hits or []
        self._cart = cart

    async def get_product(self, product_id: str) -> Product:
        return self._catalog[product_id]

    async def search_products(self, query: str, *, limit: int = 20):
        return ProductSearchResult(query=query, products=self._search_hits)

    async def get_cart(self, cart_id: str) -> Cart:
        return self._cart


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


async def add_ordinal_reference_is_never_decided_here() -> None:
    """Rev. 2 migration (2026-09): deciding WHICH product an "add" targets
    is no longer `resolve_cart_target`'s job - that decision belongs
    exclusively to `verification.py::verify()`, which sees every topic
    genuinely in view, not just this function's own active-topic-only
    `state`. This function is only ever reached for "add" after that
    unified path already declined, so it must always ask, never resolve a
    position itself against its own narrower view - the exact topic-blind
    bug this migration closed (see PROGRESS.md)."""
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(is_cart_intent=True, reference_type="ordinal", position=2)
    )
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="add the second one",
        history=None, state=_state(["P1001", "P1005"]),
    )
    check("cart intent still detected", result.is_cart_intent)
    check("an 'add' ordinal is NEVER resolved here - always asks", result.product_id is None)
    check("an honest clarifying question is returned instead", bool(result.clarifying_question))


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


async def add_pronoun_reference_is_never_decided_here() -> None:
    """Same migration as the ordinal case above - even a single-candidate
    "add it" is never resolved here for "add" any more; the unified
    `verify()` path already had its chance with the full, real
    `ConversationContext` before this function is ever reached."""
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(is_cart_intent=True, reference_type="pronoun")
    )
    single = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="add it",
        history=None, state=_state(["P1001"]),
    )
    check("a bare pronoun 'add' is never resolved here, even unambiguous", single.product_id is None)
    check("asks instead", bool(single.clarifying_question))

    ambiguous = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="add it",
        history=None, state=_state(["P1001", "P1005"]),
    )
    check("a bare pronoun with two shown products is NOT resolved (ambiguous)", ambiguous.product_id is None)
    check("ambiguous pronoun still asks which one", bool(ambiguous.clarifying_question))


async def add_name_reference_is_never_decided_here() -> None:
    """Same migration - naming a real, already-shown product for an "add"
    is no longer resolved here either. A named reference for remove/
    set_quantity (below) still resolves directly, since that targets the
    real, current cart contents - a single source of truth with no topic
    to get wrong - not a topic-scoped "which product" decision."""
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(
            is_cart_intent=True, reference_type="name", named_hint="Trailblazer"
        )
    )
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(), message="get me the Trailblazer",
        history=None, state=_state(["P1001", "P1005"]),
    )
    check("an 'add' name reference is never resolved here", result.product_id is None)
    check("asks instead", bool(result.clarifying_question))


async def remove_by_name_still_resolves_against_real_cart_lines() -> None:
    """Retained legitimate infra: "remove"/"set_quantity" still resolve
    directly here, unlike "add" above - a cart line is the real, current,
    single source of truth (never a topic-scoped guess), so there is no
    "which topic" ambiguity to get wrong the way "add" had."""
    cart = Cart(
        cart_id="cart1", currency="INR", subtotal=Money(amount="1000.00", currency="INR"),
        lines=[
            CartLine(
                line_id="l1", product_id="P1001", title="Trailblazer Running Shoe",
                quantity=1, unit_price=Money(amount="1000.00", currency="INR"),
                line_total=Money(amount="1000.00", currency="INR"),
            ),
        ],
    )
    reasoning = _ScriptedReasoning(
        CartReferenceIntent(
            is_cart_intent=True, operation="remove", reference_type="name",
            named_hint="Trailblazer",
        )
    )
    result = await resolve_cart_target(
        reasoning=reasoning, adapter=_StubAdapter(cart=cart), message="remove the Trailblazer",
        history=None, state=_state([]), cart_id="cart1",
    )
    check("remove-by-name still resolves against the real cart line", result.product_id == "P1001")
    check("operation is remove", result.operation == "remove")


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
    asyncio.run(add_ordinal_reference_is_never_decided_here())
    asyncio.run(ordinal_out_of_range_asks_instead_of_guessing())
    asyncio.run(add_pronoun_reference_is_never_decided_here())
    asyncio.run(add_name_reference_is_never_decided_here())
    asyncio.run(remove_by_name_still_resolves_against_real_cart_lines())
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
