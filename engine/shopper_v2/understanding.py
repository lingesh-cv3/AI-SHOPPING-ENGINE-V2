"""Understanding: turn one shopper message into a structured `StateDelta`.

Reuses the EXISTING `ReasoningService`/`LLMClient`/`propose_actions` tool
machinery unmodified (`engine/reasoning/service.py`, `engine/reasoning/llm.py`,
`engine/reasoning/prompts.py`) - no new prompt, no new tool schema, no new
provider code. `ReasoningService.reason()` already returns a `shopping_state`
dict parsed from the model's own structured tool call
(`ReasoningService._parse_shopping_state`); this module only reshapes that
dict into a typed `StateDelta` and enforces the two Stage 1 invariants that
matter most:

1. The model's own `reply` field is read and immediately discarded here. It
   never leaves this function. Nothing downstream of `understand()` ever
   sees it - this is the structural fix for the reply-before-data defect,
   for this one path: there is no variable carrying that text past this
   point for a caller to accidentally use.

2. The model's own `actions` field (`Reasoning.actions`, a list of
   `ProposedAction`) is read and discarded here too, with TWO narrow
   exceptions:

   - Phase 1: the first proposed action's own `action_type` (a label like
     `"COMPARE_PRODUCTS"`) is kept, as `UnderstandingResult.hinted_action_type`,
     purely for `capability.py::route`'s read-only classification.
   - Phase 4: when that first action's type is `ADD_TO_CART` or
     `PROPOSE_CART_CHANGE`, its `product_id`/`chosen_product` parameter
     (a bare string, nothing else) is ALSO kept, as
     `UnderstandingResult.hinted_product_id`. This is still not a
     proposal and still cannot execute anything by itself: it is handed
     to `cart_proposal.py`, which can only ever build a
     `PROPOSE_CART_CHANGE` candidate from it - never a direct mutation -
     and that candidate's own dispatch (`engine/execution/service.py`,
     unmodified) independently re-verifies the id against the real
     adapter before using it. No other parameter (`variant_id`,
     `quantity`, `rationale`, `confidence`) ever leaves this function, and
     `ProposedAction` itself never leaves this function.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from engine.reasoning.service import ReasoningService

logger = logging.getLogger(__name__)

_VALID_MISSION_STATUS = {"exploring", "comparing", "deciding", "done"}


class StateDelta(BaseModel):
    """One turn's proposed change to `ShoppingStateV2`. Field names mirror
    the `shopping_state` sub-object of the existing `propose_actions` tool
    (see `engine/reasoning/prompts.py::propose_tool`) - no new schema was
    authored to produce this."""

    goal: str | None = None
    clear_goal: bool = False
    budget_amount: str | None = None
    budget_currency: str | None = None
    clear_budget: bool = False
    constraints_add: list[str] = Field(default_factory=list)
    constraints_remove: list[str] = Field(default_factory=list)
    must_have_add: list[str] = Field(default_factory=list)
    must_have_remove: list[str] = Field(default_factory=list)
    nice_to_have_add: list[str] = Field(default_factory=list)
    nice_to_have_remove: list[str] = Field(default_factory=list)
    exclusions_add: list[str] = Field(default_factory=list)
    exclusions_remove: list[str] = Field(default_factory=list)
    priorities_add: list[str] = Field(default_factory=list)
    mission_status: str | None = None
    #: What to retrieve on this turn, if anything. Falls back to the
    #: shopper's raw message when the model left this blank - the same
    #: "rescue search" behaviour V1's execution layer already uses (see
    #: engine/execution/relevance.py's own module docstring).
    search_query: str | None = None


class UnderstandingResult(BaseModel):
    delta: StateDelta
    used_model: bool
    #: True only when the model was reachable AND called the tool - lets a
    #: caller distinguish "no delta because nothing changed" from "no delta
    #: because the model was unavailable", without exposing any model text.
    model_name: str | None = None
    #: The first proposed action's `action_type` string, if the model
    #: proposed one - a routing LABEL only, see this module's own
    #: docstring, invariant 2. `None` when the model proposed nothing or is
    #: unavailable; `capability.py::route` treats `None` as DISCOVER.
    hinted_action_type: str | None = None
    #: A bare product-id STRING, only ever present when
    #: `hinted_action_type` is `ADD_TO_CART`/`PROPOSE_CART_CHANGE` - see
    #: this module's own docstring, invariant 2. Never trusted directly;
    #: `cart_proposal.py` only ever uses it to build a proposal, which the
    #: existing execution pipeline independently re-verifies.
    hinted_product_id: str | None = None
    #: A bare order-id STRING, only ever present when `hinted_action_type`
    #: is `CHECK_ORDER_STATUS`. Never trusted as proof of ownership -
    #: `order_status.py` still requires the real `db.owners` check before
    #: disclosing line items, exactly as V1 already does.
    hinted_order_id: str | None = None
    #: A bare quantity INT, only ever present when `hinted_action_type` is
    #: `UPDATE_CART_QUANTITY`. Never trusted directly - re-validated by the
    #: same existing execution dispatch that already parses/validates it.
    hinted_quantity: int | None = None


async def understand(
    *,
    reasoning: ReasoningService,
    message: str,
    history: list[str] | None,
    shopping_state: dict | None,
    memory_facts: list[dict] | None,
) -> UnderstandingResult:
    """Run the existing structured-extraction call and return only the
    parts Stage 1 is allowed to use.

    Deliberately calls the SAME `ReasoningService.reason()` V1 already uses
    - no second LLM client, no new tool. `friction=None` and no
    products/cart/order are passed because Stage 1 is a pure discovery
    turn, not a friction-recovery one; `query` is left unset so the model's
    own `shopping_state.search_query`-equivalent extraction (folded into
    `reply`'s sibling fields in the existing tool) or the raw message is
    what drives retrieval - see `StateDelta.search_query`'s fallback,
    applied by the caller in `orchestrator.py`, not here.
    """
    result = await reasoning.reason(
        friction=None,
        message=message,
        history=history,
        shopping_state=shopping_state,
        memory_facts=memory_facts,
    )

    # Invariant 1: the model's reply is read here, for logging shape only,
    # and never stored in anything this function returns.
    _ = result.reply  # explicitly discarded - see module docstring

    # Invariant 2: only `action_type` (a label) is kept, from the FIRST
    # proposed action only - everything else about every proposed action,
    # including all of its own parameters, is discarded here and never
    # touched again below.
    hinted_action_type: str | None = None
    hinted_product_id: str | None = None
    hinted_order_id: str | None = None
    hinted_quantity: int | None = None
    if result.actions:
        first = result.actions[0]
        hinted_action_type = str(getattr(first, "action_type", "") or "") or None
        params = getattr(first, "parameters", None) or {}
        if hinted_action_type in (
            "ADD_TO_CART",
            "PROPOSE_CART_CHANGE",
            "REMOVE_CART_LINE",
            "UPDATE_CART_QUANTITY",
        ):
            raw_id = params.get("chosen_product") or params.get("product_id")
            if isinstance(raw_id, str) and raw_id.strip():
                hinted_product_id = raw_id.strip()
        if hinted_action_type == "UPDATE_CART_QUANTITY":
            raw_qty = params.get("quantity")
            if isinstance(raw_qty, int) and raw_qty >= 0:
                hinted_quantity = raw_qty
            elif isinstance(raw_qty, str) and raw_qty.isdigit():
                hinted_quantity = int(raw_qty)
        if hinted_action_type == "CHECK_ORDER_STATUS":
            raw_order = params.get("order_id")
            if isinstance(raw_order, str) and raw_order.strip():
                hinted_order_id = raw_order.strip()
        logger.debug(
            "shopper_v2 understanding: discarding %d proposed action(s); "
            "keeping only action_type=%r (+hints) as routing labels",
            len(result.actions),
            hinted_action_type,
        )

    raw_state = result.shopping_state or {}
    clears = set(raw_state.get("clear_fields") or [])

    delta = StateDelta(
        goal=raw_state.get("goal"),
        clear_goal="goal" in clears,
        budget_amount=(
            str(raw_state["budget_amount"]) if "budget_amount" in raw_state else None
        ),
        budget_currency=raw_state.get("budget_currency"),
        clear_budget="budget_amount" in clears or "budget_currency" in clears,
        constraints_add=raw_state.get("constraints_add") or [],
        constraints_remove=raw_state.get("constraints_remove") or [],
        must_have_add=raw_state.get("must_have_add") or [],
        must_have_remove=raw_state.get("must_have_remove") or [],
        nice_to_have_add=raw_state.get("nice_to_have_add") or [],
        nice_to_have_remove=raw_state.get("nice_to_have_remove") or [],
        exclusions_add=raw_state.get("exclusions_add") or [],
        exclusions_remove=raw_state.get("exclusions_remove") or [],
        priorities_add=raw_state.get("priorities_add") or [],
        mission_status=(
            raw_state["mission_status"]
            if raw_state.get("mission_status") in _VALID_MISSION_STATUS
            else None
        ),
        # Always None for Stage 1: the existing `propose_actions` tool only
        # carries a search term inside each proposed ACTION's own
        # parameters (`ProposedAction.parameters["query"]`), not in the
        # `shopping_state` sub-object this module reads - and `actions` is
        # exactly what this module discards (see module docstring,
        # invariant 2). `orchestrator.py` falls back to the shopper's raw
        # message for retrieval instead. A later stage that wants a
        # model-extracted search term without reopening `actions` would
        # need its own narrow field for it - not part of Stage 1.
        search_query=None,
    )
    return UnderstandingResult(
        delta=delta,
        used_model=result.used_model,
        model_name=result.model_name,
        hinted_action_type=hinted_action_type,
        hinted_product_id=hinted_product_id,
        hinted_order_id=hinted_order_id,
        hinted_quantity=hinted_quantity,
    )
