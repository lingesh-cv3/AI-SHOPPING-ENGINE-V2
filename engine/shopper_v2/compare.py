"""Phase 3, first slice: Comparison.

Reads `ShoppingStateV2.active_products` (populated by `orchestrator.py`
after a discovery turn - see its own comment) to resolve "compare the top
two" without needing the model's discarded `ProposedAction` parameters.
Both products are ALWAYS fetched fresh via `adapter.get_product` - never
recalled from anything cached earlier in the conversation - mirroring V1's
own COMPARE_PRODUCTS guarantee (Completed.md #27) exactly, reused rather
than re-derived.

Read-only: calls only `adapter.get_product` (the same
`StandardCommerceInterface` read method V1's `COMPARE_PRODUCTS` already
uses) and `ReasoningService.synthesize(kind="comparison", ...)` (the same
existing, narrow, tool-free synthesis method `respond.py` already uses for
discovery). Never imports `engine.decision`, `engine.risk`, or any
`engine.execution` mutation path.
"""

from __future__ import annotations

import logging

from engine.reasoning.service import ReasoningService
from shared.models import Product

from .evidence import ProductEvidence, TurnEvidence
from .respond import _claims_a_cart_action
from .state import ShoppingStateV2

logger = logging.getLogger(__name__)

_NEED_TWO_REPLY = (
    "I don't have two products in view to compare yet - tell me what you're "
    "looking for first, and I can compare the top options once you've seen some."
)
_FETCH_FAILED_REPLY = (
    "I couldn't fetch both of those products to compare them right now - "
    "please try again in a moment."
)


async def run_compare_turn(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    state: ShoppingStateV2,
) -> tuple[str, TurnEvidence, list[Product], bool]:
    """Returns (reply, evidence, fetched products, used_model)."""
    ids = list(dict.fromkeys(state.active_products))[:2]
    if len(ids) < 2:
        return (_NEED_TWO_REPLY, TurnEvidence(query=None, products=[]), [], False)

    products: list[Product] = []
    for pid in ids:
        try:
            products.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001 - degrade honestly, never raise to the shopper
            logger.warning("shopper_v2 compare: get_product failed for %r", pid, exc_info=True)

    if len(products) < 2:
        return (_FETCH_FAILED_REPLY, TurnEvidence(query=None, products=[]), products, False)

    # Every fetched product is real, by construction - `exists=True` for
    # both, unconditionally verified relevant (a shopper-named comparison
    # target is not a discovery guess to re-score against a query).
    evidence = TurnEvidence(
        query=None,
        products=[
            ProductEvidence(
                product_id=p.product_id,
                exists=True,
                hard_constraints_passed=True,
                relevance_verified=True,
                relevance_score=0,
                weak_match=False,
                evidence_fields=["shopper_named_comparison_target"],
            )
            for p in products
        ],
    )

    items = [
        {
            "product_id": p.product_id,
            "title": p.title,
            "price": str(p.price) if p.price is not None else None,
            "availability": str(p.availability),
            "description": p.description,
        }
        for p in products
    ]
    synthesis = await reasoning.synthesize(
        kind="comparison",
        message=message,
        items=items,
        shopping_state=state.as_prompt_dict(),
    )
    if synthesis.text and not _claims_a_cart_action(synthesis.text):
        return (synthesis.text, evidence, products, synthesis.used_model)

    return (_deterministic_comparison(products), evidence, products, False)


def _deterministic_comparison(products: list[Product]) -> str:
    a, b = products[0], products[1]
    parts = [f"{a.title}" + (f" ({a.price})" if a.price else "")]
    parts.append(f"{b.title}" + (f" ({b.price})" if b.price else ""))
    return "Here are both, fetched just now: " + " vs. ".join(parts) + "."
