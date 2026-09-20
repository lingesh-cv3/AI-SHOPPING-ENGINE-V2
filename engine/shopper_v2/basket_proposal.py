"""Multi-item basket proposals: turning a multi-item shopping mission's
independently-tracked, independently-verified items into real cart
proposals - one per item, never a single fabricated "bundle" object, and
never more than one product per item.

**Reuses the existing single-item proposal machinery completely - this
module builds no new commerce primitive.** Every item's proposal is
created through the SAME `cart_proposal.py::run_cart_change_proposal_turn`
Phase 4 already built and this codebase already trusts (Decision Engine ->
Risk Gate -> `db.record_case` -> `ExecutionService.execute_case`, always
and only an `ActionType.PROPOSE_CART_CHANGE` candidate, never a direct
mutation). A "basket" is not a new stored concept: it is N ordinary
`CartProposal` rows, tagged with the mission item they fulfill via the
existing, additive `mission_item_id` column, and every one of them already
shows up wherever a shopper's pending proposals already show up
(`GET /api/chat/proposals` - unmodified, un-aware this module exists) -
because that route already lists every PENDING proposal for the cart, not
just ones created through V1's own path.

**The one hard rule this module enforces, in code, matching
`cart_proposal.py`'s own rule for a single item:** never propose a product
for a mission item unless that item has EXACTLY ONE real,
relevance-verified candidate. Zero candidates is reported honestly as
"nothing found for X yet" - never proposed. More than one candidate is
reported honestly as "you'll need to choose for X" - never guessed at,
because doing so would substitute the shopper's own choice with the
model's, exactly the anti-pattern the whole cart-proposal mechanism exists
to prevent. An item already `done` (settled) is left alone - proposing
again for something already decided would silently second-guess a
shopper's own prior decision.

This module never imports `ActionType`/`ProposedAction` itself - it only
calls `cart_proposal.py`'s existing function, which is the sole place in
`engine/shopper_v2/` allowed to construct a `PROPOSE_CART_CHANGE`
candidate. See that module's own docstring for the full safety argument
this one inherits unchanged.
"""

from __future__ import annotations

import logging

from engine.db import shopping_state as shopping_state_store
from engine.decision import DecisionEngine
from engine.execution import ExecutionService
from engine.risk import RiskGate

from .cart_proposal import run_cart_change_proposal_turn
from .state import MissionItem, ShoppingStateV2

logger = logging.getLogger(__name__)


class BasketItemResult:
    def __init__(
        self,
        *,
        item: MissionItem,
        outcome: str,  # "proposed" | "no_candidates" | "ambiguous" | "already_settled" | "failed"
        proposal: dict | None = None,
    ) -> None:
        self.item = item
        self.outcome = outcome
        self.proposal = proposal


async def run_basket_proposal_turn(
    *,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    adapter,
    connection_id: str,
    session_id: str,
    cart_id: str | None,
    message: str,
    state: ShoppingStateV2,
) -> tuple[str, list, ShoppingStateV2, list[BasketItemResult]]:
    """Propose every eligible tracked item's product, independently.
    Returns `(reply, products, new_state, results)`. `results` names, per
    item, exactly what happened - every finding above is grounded in a
    real outcome, never inferred.

    Persists `selected_product_id`/`status` on each successfully-proposed
    item (never on an ambiguous or empty one - nothing was chosen for
    those) so a later "compare the shoes and the socks I picked" or a
    repeat basket-propose call can see what is already pending, without
    re-deriving it from the proposal store itself.
    """
    if not cart_id:
        return (
            "I don't have a cart to propose this basket into yet - please start one first.",
            [],
            state,
            [],
        )

    eligible = [i for i in state.mission_items if i.status != "done"]
    if not eligible:
        return (
            "Nothing left to propose - every tracked item is already settled.",
            [],
            state,
            [],
        )

    results: list[BasketItemResult] = []
    for item in eligible:
        if not item.candidates:
            results.append(BasketItemResult(item=item, outcome="no_candidates"))
            continue
        if len(item.candidates) > 1:
            results.append(BasketItemResult(item=item, outcome="ambiguous"))
            continue

        product_id = item.candidates[0]
        reply_text, proposal = await run_cart_change_proposal_turn(
            decision=decision,
            gate=gate,
            execution=execution,
            connection_id=connection_id,
            session_id=session_id,
            cart_id=cart_id,
            message=message,
            state=state,
            operation="add",
            hinted_product_id=product_id,
            mission_item_id=item.item_id,
            reason=f"part of your {item.label} item",
        )
        if proposal is None:
            logger.info(
                "basket_proposal: could not create a proposal for item %r (%s)",
                item.item_id, reply_text,
            )
            results.append(BasketItemResult(item=item, outcome="failed"))
            continue

        results.append(BasketItemResult(item=item, outcome="proposed", proposal=proposal))

    # Persist the tentative pick on each successfully-proposed item -
    # deep-copied, same discipline as missions.py's own mutation handling,
    # so this never touches the caller's original state objects.
    updated_by_id = {i.item_id: i.model_copy(deep=True) for i in state.mission_items}
    for result in results:
        if result.outcome != "proposed":
            continue
        live = updated_by_id[result.item.item_id]
        live.selected_product_id = result.proposal["product_id"]
        live.status = "deciding"

    all_items = list(updated_by_id.values())
    await shopping_state_store.merge(
        state.session_id,
        state.connection_id,
        mission_items=[i.model_dump(mode="json") for i in all_items],
    )
    new_state = state.model_copy(update={"mission_items": all_items})

    products = []
    for result in results:
        if result.outcome != "proposed":
            continue
        try:
            products.append(await adapter.get_product(result.proposal["product_id"]))
        except Exception:  # noqa: BLE001
            logger.warning(
                "basket_proposal: get_product failed for %r",
                result.proposal["product_id"], exc_info=True,
            )

    reply = _build_reply(results)
    return reply, products, new_state, results


def _build_reply(results: list[BasketItemResult]) -> str:
    proposed = [r for r in results if r.outcome == "proposed"]
    no_candidates = [r for r in results if r.outcome == "no_candidates"]
    ambiguous = [r for r in results if r.outcome == "ambiguous"]
    failed = [r for r in results if r.outcome == "failed"]

    lines: list[str] = []
    if proposed:
        labels = ", ".join(r.item.label for r in proposed)
        lines.append(
            f"I've proposed adding: {labels}. Confirm each from your pending "
            "suggestions to add it to your cart."
        )
    if ambiguous:
        labels = ", ".join(r.item.label for r in ambiguous)
        lines.append(f"You'll need to pick which one for: {labels}.")
    if no_candidates:
        labels = ", ".join(r.item.label for r in no_candidates)
        lines.append(f"I haven't found anything for: {labels} yet.")
    if failed:
        labels = ", ".join(r.item.label for r in failed)
        lines.append(f"I couldn't propose: {labels} - please try again.")

    return " ".join(lines) if lines else "Nothing to propose right now."
