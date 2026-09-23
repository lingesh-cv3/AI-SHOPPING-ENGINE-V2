"""Trade-off reasoning and re-ranking: "comfort matters more than price,"
then later "actually durability matters more" - a stated priority change
must re-order what's currently under consideration and explain the new
order using real, grounded reasons, not just quietly accept the statement
into `ShoppingStateV2.priorities` and say nothing about its effect.

**Introduces no new ranking logic.** The priority-change signal itself is
not a new bounded LLM call either - `priorities_add` already arrives on
every turn's `StateDelta` via the EXISTING `understand()` call
(`engine/reasoning/service.py::reason()`'s own `shopping_state.priorities_add`
field, unmodified, already flowing into `ShoppingStateV2.priorities` via
`state.py::apply_delta` before this module is ever reached). Re-ranking
and the grounded "why" both come from calling the EXISTING
`engine.execution.mission_ranking.rank_and_filter` a second time, with the
shopper's now-updated `priorities` - the same function `evidence.py`
already calls for every discovery turn, and the same `fit_reasons` shape
`respond.py`/V1's own honest-synthesis discipline already trusts as
grounded (real field, real phrase match) rather than invented. This
module's only job is deciding WHEN to make that second call and how to
report its result plainly - it does not re-derive or duplicate the
ranking itself.

Read-only: calls only `adapter.get_product` (real, already-verified
`active_products`, never a fresh guess) and the existing, unmodified
`mission_ranking.rank_and_filter`. Never imports `engine.decision`,
`engine.risk`, or any `engine.execution` mutation path - a priority
statement changes what is SHOWN and in what ORDER, never the cart.
"""

from __future__ import annotations

import logging

from engine.execution import mission_ranking
from shared.models import Product

from .evidence import TurnEvidence, already_verified_evidence, product_as_dict
from .state import ShoppingStateV2, recent_relevant_product_ids

logger = logging.getLogger(__name__)

_NOTHING_TO_RERANK_REPLY = (
    "Noted for next time - I don't have anything currently in view to "
    "re-rank against that yet."
)
_FETCH_FAILED_REPLY = (
    "I couldn't re-check those against your new priority right now - "
    "please try again in a moment."
)


async def run_tradeoff_turn(
    *,
    adapter,
    message: str,
    state: ShoppingStateV2,
) -> tuple[str, TurnEvidence, list[Product], list[str]]:
    """Re-rank `state.active_products` using the CURRENT (just-updated)
    `state.priorities`/`must_have`/`constraints`/`exclusions`/`budget`, and
    explain the result using only real, grounded reasons
    `mission_ranking.rank_and_filter` returns for the new top product.

    Returns `(reply, evidence, reordered_products, new_active_product_ids)`.
    The caller (`orchestrator.py`) is responsible for persisting
    `new_active_product_ids` as the session's new `active_products` order,
    exactly as a discovery turn already does - this function itself never
    writes to storage, matching `compare.py`'s own read-only discipline.
    """
    # Batch 2: only reaches into ledger history when `active_products`
    # alone is genuinely empty (minimum=1) - re-ranking deliberately stays
    # scoped to the CURRENT single topic whenever one exists, since
    # blending two unrelated topics (shoes and slides) into one re-rank
    # would conflate categories rather than correctly resolve historical
    # context. See `state.recent_relevant_product_ids`'s own docstring.
    ids = recent_relevant_product_ids(state, minimum=1)
    if not ids:
        return (_NOTHING_TO_RERANK_REPLY, TurnEvidence(query=None, products=[]), [], [])

    products: list[Product] = []
    for pid in ids:
        try:
            products.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001 - degrade honestly, never raise to the shopper
            logger.warning("shopper_v2 tradeoff: get_product failed for %r", pid, exc_info=True)

    if not products:
        return (_FETCH_FAILED_REPLY, TurnEvidence(query=None, products=[]), [], [])

    product_dicts = [product_as_dict(p) for p in products]
    shortlist, fit_reasons = mission_ranking.rank_and_filter(
        product_dicts, state.as_prompt_dict(), limit=len(product_dicts)
    )

    by_id = {p.product_id: p for p in products}
    reordered_ids = [d["product_id"] for d in shortlist if d["product_id"] in by_id]
    # Anything that dropped out of the shortlist (a hard filter - exclusion/
    # budget/out-of-stock) is genuinely no longer relevant against the new
    # statement, not merely re-ordered - it is left out of what is shown,
    # never silently kept and re-labelled as still fitting.
    reordered_products = [by_id[pid] for pid in reordered_ids]

    # Shared contract with `compare.py` - see
    # `evidence.already_verified_evidence`'s own docstring. The hard
    # filtering already happened above (`rank_and_filter`); this only
    # packages its real survivors with their real, grounded reasons.
    evidence = already_verified_evidence(
        reordered_products,
        {p.product_id: fit_reasons.get(p.product_id, []) for p in reordered_products},
    )

    if not reordered_products:
        return (
            "None of what was shown still fits with that - want me to look for something else?",
            evidence,
            [],
            [],
        )

    reply = _build_reply(reordered_products, fit_reasons)
    return (reply, evidence, reordered_products, reordered_ids)


def _build_reply(products: list[Product], fit_reasons: dict[str, list[str]]) -> str:
    top = products[0]
    reasons = fit_reasons.get(top.product_id) or []
    if reasons:
        why = "; ".join(reasons[:2])
        lead = f"With that in mind, {top.title} now fits best - {why}."
    else:
        lead = f"With that in mind, {top.title} still fits best."
    if len(products) > 1:
        rest = ", ".join(p.title for p in products[1:3])
        lead += f" Next: {rest}."
    return lead
