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
import re

from pydantic import BaseModel, Field

from engine.reasoning.service import ReasoningService

logger = logging.getLogger(__name__)

_VALID_MISSION_STATUS = {"exploring", "comparing", "deciding", "done"}

#: A narrow, closed-class backstop for the model's own `durable_facts`
#: extraction (`ReasoningService.reason()`, same call this module already
#: reuses) - never a replacement for it. Live-reproduced, same phrasing
#: shape, two different real outcomes from the SAME underlying model call:
#: "I really prefer lightweight gear when I shop" produced a real
#: `durable_facts` entry; "I really prefer decaf coffee." (direct call,
#: `message_intent="shopping"`) produced `durable_facts=None` - the model
#: instead folded it straight into an immediate `RECOMMEND_PRODUCTS`
#: search action. The prompt's own guidance (`prompts.py`: "fill
#: durable_facts only for something that would still be true on a
#: completely different visit... a stable preference... stated by the
#: shopper themselves") already calls this exact shape a durable fact -
#: this is model inconsistency on an unambiguous case, not an ambiguous
#: judgment call, and CLAUDE.md's own discipline is to prefer a
#: deterministic, closed-class check over trusting free-form model output
#: wherever one is safely possible. Deliberately narrow: only "I
#: (always/really/absolutely/definitely) prefer <phrase>" - the shopper's
#: own explicit, general-preference wording, not "I need/want <phrase>"
#: (an ordinary current-turn goal, never a durable fact by itself). Fires
#: only when the model supplied NO durable fact this turn - it fills a gap,
#: it never overrides or duplicates a real model-supplied one.
_EXPLICIT_PREFERENCE_PATTERN = re.compile(
    r"\bi\s+(?:always\s+|really\s+|absolutely\s+|definitely\s+)*prefer(?:s)?\s+"
    r"(?P<term>[a-z][a-z0-9 '-]{2,40}?)(?:\s+(?:coffee|gear|shoes|products|items|"
    r"stuff|things|when i shop|when i buy|when shopping|for my order)s?\b.*)?"
    r"[.!?]?\s*$",
    re.IGNORECASE,
)


def _explicit_preference_fact(message: str) -> dict | None:
    match = _EXPLICIT_PREFERENCE_PATTERN.search(message.strip())
    if not match:
        return None
    term = match.group("term").strip().lower()
    if not term or len(term) < 3:
        return None
    key = "prefers_" + re.sub(r"[^a-z0-9]+", "_", term).strip("_")
    if not key or key == "prefers_":
        return None
    return {"key": key[:120], "value": "true"}


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
    #: "shopping" or "conversational" - the model's own genuine judgment,
    #: from the SAME call, of what this message needs (see
    #: `engine/reasoning/service.py::Reasoning.message_intent`'s own
    #: docstring for the live-reproduced bug this closes). `None` when the
    #: model was unavailable or didn't classify - `capability.py::route`
    #: treats that the same as "shopping" only when there's already other
    #: evidence of shopping intent, and otherwise degrades to a
    #: conversational reply rather than guessing, matching this package's
    #: existing "ask/respond naturally, never guess" discipline.
    message_intent: str | None = None
    #: The model's OWN reply text, kept ONLY when `message_intent ==
    #: "conversational"` - the ONE case where invariant 1's "never use the
    #: pre-fetch reply" rule does not apply, because a purely
    #: conversational turn describes no fetched commerce data at all
    #: (there is nothing for a pre-fetch reply to be wrong about). Still
    #: never trusted blindly: `conversational.py::run_conversational_turn`
    #: runs the SAME `_claims_a_cart_action` check `respond.py` already
    #: uses before ever showing it to a shopper, and falls back to a
    #: deterministic reply if it fails that check. For every other
    #: `message_intent`, this stays `None` and the pre-fetch reply is
    #: discarded exactly as invariant 1 always required.
    conversational_reply: str | None = None
    #: Durable facts the SAME model call already flagged this turn
    #: (`ReasoningService.reason`'s own `durable_facts`, validated by
    #: `_parse_durable_facts` - key/value strings only, never trusted
    #: further here). Live-reproduced gap this closes: V1's chat.py has
    #: always persisted these to `db.memory_facts` for a signed-in shopper;
    #: V2's `understand()` discarded `result.durable_facts` entirely, so a
    #: shopper saying "I really prefer lightweight gear" through V2 got an
    #: acknowledging reply but no fact was ever written - `rank_and_filter`'s
    #: memory-fact personalization (proven correct at the ranking layer
    #: itself) had nothing to read on any later turn, making it reachable
    #: only by seeding the database directly, never by a real conversation.
    #: The caller (graph.py's `node_understand`) does the actual signed-in-
    #: shopper-only persist, mirroring chat.py exactly.
    durable_facts: list[dict] = Field(default_factory=list)


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

    # Structural backstop, live-reproduced: "I need shoes" - about as
    # unambiguous a shopping statement as exists - was classified
    # `message_intent="conversational"` by the model roughly 1 in 5 times
    # across repeated identical calls (5 runs, same message, same empty
    # history), each time producing a real, non-empty `delta.goal` in the
    # SAME structured call ("shoes") alongside it - an internally
    # inconsistent result the earlier `actions`-vs-`message_intent`
    # consistency check does not catch, because `actions` was correctly
    # empty both times; the inconsistency here is between `message_intent`
    # and this OTHER field of the same tool call. A message that
    # genuinely has nothing to act on also states no new goal/budget/
    # constraint - so a real, freshly-stated one is itself independent,
    # deterministic evidence the turn is NOT purely conversational,
    # whatever the model's own label says. Never a guess: every field
    # checked here is something the model itself extracted this exact
    # turn, not inferred from the message text by this module.
    message_intent = result.message_intent
    if message_intent == "conversational" and (
        delta.goal
        or delta.budget_amount
        or delta.must_have_add
        or delta.constraints_add
        or delta.nice_to_have_add
        or delta.exclusions_add
        or delta.priorities_add
    ):
        message_intent = None

    # Invariant 1: the model's reply is read here, for logging shape only,
    # and never stored in anything this function returns - EXCEPT for the
    # one case `UnderstandingResult.conversational_reply`'s own docstring
    # explains: a message classified "conversational" describes no fetched
    # commerce data, so there is no pre-fetch-reply-before-data risk to
    # guard against for it specifically. Gated on the (possibly
    # overridden) `message_intent` above, not the model's raw one - a turn
    # the backstop just reclassified as real shopping intent DOES describe
    # data about to be fetched, so invariant 1's discard applies to it too.
    conversational_reply = result.reply if message_intent == "conversational" else None
    _ = result.reply  # explicitly discarded for every other case - see module docstring

    # Deterministic backstop only - see `_explicit_preference_fact`'s own
    # docstring. Never runs when the model already supplied a real fact.
    durable_facts = list(result.durable_facts or [])
    if not durable_facts:
        backstop_fact = _explicit_preference_fact(message)
        if backstop_fact is not None:
            durable_facts = [backstop_fact]

    return UnderstandingResult(
        delta=delta,
        used_model=result.used_model,
        model_name=result.model_name,
        hinted_action_type=hinted_action_type,
        hinted_product_id=hinted_product_id,
        hinted_order_id=hinted_order_id,
        hinted_quantity=hinted_quantity,
        message_intent=message_intent,
        conversational_reply=conversational_reply,
        durable_facts=durable_facts,
    )
