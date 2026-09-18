"""Response generation for Stage 1.

THE MOST IMPORTANT FILE IN THIS PACKAGE, per the approved spec: this
function's signature is the structural enforcement of "no shopper-facing
response may be generated from the pre-fetch Understanding model response."

`respond()` takes only:
  - `evidence`  (engine.shopper_v2.evidence.TurnEvidence - built AFTER retrieval)
  - `products`  (the real, adapter-fetched Product objects)
  - `state`     (the current, already-updated ShoppingStateV2)

It has no parameter through which a pre-fetch model reply could arrive, by
construction - `engine/shopper_v2/understanding.py::understand` never
returns one in the first place (see that module's docstring), so there is
nothing to pass here even if a future edit tried.
"""

from __future__ import annotations

from engine.reasoning.service import ReasoningService
from shared.models import Product

from .evidence import TurnEvidence
from .state import ShoppingStateV2


async def respond(
    *,
    reasoning: ReasoningService,
    message: str,
    evidence: TurnEvidence,
    products: list[Product],
    state: ShoppingStateV2,
) -> tuple[str, bool]:
    """Return (reply_text, used_model). Ordering is enforced by the caller
    (`orchestrator.py`) never invoking this before evidence/products exist -
    this function has no other way to be called."""
    verified = [p for e, p in _pair(evidence, products) if e.relevance_verified]

    if evidence.no_match_reason == "platform_unavailable":
        return (
            "I couldn't reach the store's catalog just now - please try again "
            "in a moment.",
            False,
        )

    if not verified:
        return (_honest_no_match(evidence, state), False)

    # Reuse the EXISTING post-execution synthesis pass - the same one V1
    # already uses for COMPARE_PRODUCTS/RECOMMEND_PRODUCTS
    # (`ReasoningService.synthesize`) - passed only the verified products
    # and current state, never the pre-fetch reply.
    items = [
        {
            "product_id": p.product_id,
            "title": p.title,
            "price": str(p.price) if p.price is not None else None,
            "description": p.description,
        }
        for p in verified
    ]
    synthesis = await reasoning.synthesize(
        kind="discovery",
        message=message,
        items=items,
        shopping_state=state.as_prompt_dict(),
    )
    if synthesis.text:
        return (synthesis.text, synthesis.used_model)

    return (_deterministic_summary(verified), False)


def _pair(evidence: TurnEvidence, products: list[Product]):
    by_id = {p.product_id: p for p in products}
    for pe in evidence.products:
        product = by_id.get(pe.product_id)
        if product is not None:
            yield pe, product


def _honest_no_match(evidence: TurnEvidence, state: ShoppingStateV2) -> str:
    if evidence.no_match_reason == "no_candidates":
        if evidence.shop_carries_category is False:
            return "I couldn't find anything like that here - this store doesn't seem to carry it."
        return "I couldn't find anything matching that search right now."
    if evidence.no_match_reason == "no_relevant_match":
        if state.budget_amount:
            return (
                "I found some results, but none that actually match what you're "
                "looking for within your stated requirements - nothing here is a "
                "confident match, so I'd rather say that than guess."
            )
        return (
            "I found some results, but none of them are a confident match for "
            "what you asked - I don't want to show you something that only "
            "shares a word with your request."
        )
    return "I couldn't confirm a good match for that."


def _deterministic_summary(verified: list[Product]) -> str:
    names = ", ".join(p.title for p in verified[:4])
    return f"I found {len(verified)} match(es): {names}."
