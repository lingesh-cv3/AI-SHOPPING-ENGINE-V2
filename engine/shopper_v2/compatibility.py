"""Phase 3 continuation: Compatibility and Complementary reasoning.

Reuses `engine.execution.product_relations` (pure, deterministic, already
V1's own source of truth for both questions) unmodified - both products
fetched fresh via `adapter.get_product`, same "never a stale record"
guarantee `compare.py` already established. Read-only: no `engine.decision`,
`engine.risk`, or `engine.execution` mutation path imported or called.

Per `product_relations.py`'s own docstring, there is no path from
"unknown" to "not_compatible" - absence of a declared relationship is
never presented as a confirmed incompatibility, only as genuinely unknown.
"""

from __future__ import annotations

import logging

from engine.execution import product_relations
from engine.reasoning.service import ReasoningService
from shared.models import Product

from .evidence import ProductEvidence, TurnEvidence
from .respond import _claims_a_cart_action
from .state import ShoppingStateV2

logger = logging.getLogger(__name__)

_NEED_TWO_REPLY = (
    "I don't have two products in view to check compatibility between - "
    "ask me to find something first."
)
_NEED_ONE_REPLY = (
    "I don't have a product in view to find complements for - ask me to "
    "find something first."
)
_FETCH_FAILED_REPLY = "I couldn't fetch that product right now - please try again."


def _evidence_for(ids: list[str]) -> TurnEvidence:
    return TurnEvidence(
        query=None,
        products=[
            ProductEvidence(
                product_id=pid,
                exists=True,
                hard_constraints_passed=True,
                relevance_verified=True,
                relevance_score=0,
                weak_match=False,
                evidence_fields=["shopper_named_target"],
            )
            for pid in ids
        ],
    )


async def run_compatibility_turn(
    *, reasoning: ReasoningService, adapter, message: str, state: ShoppingStateV2
) -> tuple[str, TurnEvidence, list[Product], bool]:
    ids = list(dict.fromkeys(state.active_products))[:2]
    if len(ids) < 2:
        return (_NEED_TWO_REPLY, TurnEvidence(query=None, products=[]), [], False)

    products: list[Product] = []
    for pid in ids:
        try:
            products.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001
            logger.warning("compatibility: get_product failed for %r", pid, exc_info=True)
    if len(products) < 2:
        return (_FETCH_FAILED_REPLY, TurnEvidence(query=None, products=[]), products, False)

    result = product_relations.check_compatibility(products[0], products[1])
    evidence = _evidence_for(ids)
    return (result.reason, evidence, products, False)


async def run_complementary_turn(
    *, reasoning: ReasoningService, adapter, message: str, state: ShoppingStateV2
) -> tuple[str, TurnEvidence, list[Product], bool]:
    ids = list(dict.fromkeys(state.active_products))
    if not ids:
        return (_NEED_ONE_REPLY, TurnEvidence(query=None, products=[]), [], False)
    anchor_id = ids[0]
    try:
        anchor = await adapter.get_product(anchor_id)
    except Exception:  # noqa: BLE001
        return (_FETCH_FAILED_REPLY, TurnEvidence(query=None, products=[]), [], False)

    related_ids = product_relations.complementary_product_ids(anchor)
    if not related_ids:
        evidence = _evidence_for([anchor_id])
        return (
            f"I don't have any declared complementary products for {anchor.title} - "
            "I won't guess at something that might not actually go with it.",
            evidence,
            [anchor],
            False,
        )

    related: list[Product] = []
    for pid in related_ids:
        try:
            related.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001
            logger.warning("complementary: get_product failed for %r", pid, exc_info=True)

    all_products = [anchor, *related]
    evidence = _evidence_for([anchor_id, *[p.product_id for p in related]])

    if not related:
        return (
            f"I don't have any declared complementary products for {anchor.title} "
            "available right now.",
            evidence,
            [anchor],
            False,
        )

    items = [
        {"product_id": p.product_id, "title": p.title, "description": p.description}
        for p in related
    ]
    synthesis = await reasoning.synthesize(
        kind="discovery",
        message=message,
        items=items,
        shopping_state=state.as_prompt_dict(),
    )
    if synthesis.text and not _claims_a_cart_action(synthesis.text):
        return (synthesis.text, evidence, all_products, synthesis.used_model)

    names = ", ".join(p.title for p in related)
    return (
        f"Declared to go with {anchor.title}: {names}.",
        evidence,
        all_products,
        False,
    )
