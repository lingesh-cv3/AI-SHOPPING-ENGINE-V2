"""Understanding: turn one shopper message into a structured `StateDelta`,
AND the turn's single authoritative interpretation claim.

Production architecture (2026-09), Phase 1 reconciliation: this module used
to call `ReasoningService.reason()` (V1's `propose_actions` tool) as a
separate Stage 1 extraction, independent of `engine/shopper_v2/
interpretation.py`'s own `interpret_turn()` call that ran later, conditionally,
inside `graph.py::node_route_capability`. That left two independent,
non-communicating interpretation authorities per turn (production
architecture review, Gap B) - this module now calls `interpretation.
interpret_turn()` (Rev. 2's own, separately-hardened `interpret_conversation`
tool, WIDENED to also carry the `shopping_state` delta - see
`engine/reasoning/prompts.py::interpretation_tool()`'s own comment) instead,
making it THE single authoritative call for the turn's entire meaning:
capability, topic/reference resolution, cart intent, question shape,
conversational repair, durable facts, AND the mission-state delta, all from
one real model round trip.

This is NOT a third interpretation layer - `interpretation.py::
interpret_turn()` is unchanged as the orchestration/formatting layer; this
module is still exactly the same Stage 1 call site `graph.py::node_understand`
already invoked, just pointed at the newer, more general call instead of the
older, narrower one. V1 (`engine/api/chat.py`) is completely unaffected: it
still calls `ReasoningService.reason()` directly, unchanged, on its own
separate, frozen code path.

Two Stage 1 invariants still apply, exactly as before:

1. The model's own `rationale` field (interpretation's equivalent of
   `reason()`'s `reply`) is never treated as shopper-facing text - the
   reply-before-data structural fix holds for this call the same way it
   held for the old one, by construction: `InterpretationResult` has no
   field a caller could mistake for a pre-fetch reply at all (unlike
   `reason()`, which HAD a `reply` field this module had to explicitly
   discard).

2. `capability`/`cart_operation`/`reference_*` are CHECKABLE CLAIMS only -
   `graph.py::node_understand` calls `verification.py::verify()` against
   real data before anything downstream acts on them, exactly as
   `node_route_capability` used to; this module itself never resolves a
   product id or executes anything.

`UnderstandingResult.hinted_action_type`/`hinted_product_id`/
`hinted_order_id`/`hinted_quantity` (the old, V1-ActionType-shaped weak
hints `capability.py::route()`/`cart_proposal.py`/`order_status.py` could
fall back to) have no source anymore - there is no more `actions[]` list a
second, independent call could produce. `route()` already treated these as
optional, re-verified-downstream hints, never authoritative on their own;
losing them narrows one already-weak fallback path (disclosed, not hidden)
rather than removing anything authoritative. The routing signal that
matters - `capability`/`cart_operation` - is now ALWAYS available whenever
this call succeeds at all, never only on a lucky partial success of one of
two independent calls.

Similarly, no shopper-facing reply text is produced by this call anymore
(the merged schema deliberately has no such field - see invariant 1 above);
`conversational.py::run_conversational_turn`'s own deterministic,
context-aware fallback (unchanged) answers a purely conversational turn
now, matching the same "never a raw pre-fetch reply" discipline every other
capability's synthesis already follows.
"""

from __future__ import annotations

import logging
import re

from pydantic import BaseModel, Field

from engine.reasoning.service import InterpretationResult, ReasoningService

from . import interpretation as interpretation_module
from .conversation_context import ConversationContext

logger = logging.getLogger(__name__)

#: Rev. 2 architecture (2026-09), Phase 6 - closed-form key normalization
#: for a durable-fact SUBJECT (never for deciding whether something IS a
#: durable fact - that decision is `ReasoningService.interpret()`'s own
#: genuine interpretation, open-vocabulary by design per the approved
#: spec §7.3). Purely mechanical: lowercase, non-alphanumeric runs become
#: underscores, bounded length - the same storage-layer normalization
#: `ShopperMemoryFact.upsert`'s own key parameter already expects, applied
#: here so two similarly-worded subjects ("coffee preference" / "Coffee
#: Preference!") land on the same key rather than accumulating as
#: separate rows.
def _normalize_fact_key(subject: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", subject.strip().lower()).strip("_")[:120]

_VALID_MISSION_STATUS = {"exploring", "comparing", "deciding", "done"}

# Production architecture (2026-09), Phase 1 reconciliation: the Phase 6
# durable-fact backstop (`_interpretation_durable_fact_backstop`) that used
# to live here is retired outright, not replaced - it existed specifically
# to cover the gap case where `reason()`'s own extraction missed a durable
# fact `interpret()` (a SEPARATE call) would have caught. Now that
# `understand()` itself calls `interpret()` directly as the turn's one
# authoritative call, `claim.durable_fact_candidates` is already the
# primary extraction, not a fallback one - there is no more gap for a
# second call to backstop.


#: Root-cause fix (2026-09): a stated hard budget number must never
#: silently disappear. Closed-class budget-signal words (the exact
#: pattern `_BUDGET_WORDS`/`_budget_disclaimer_if_needed` in answer.py
#: already treat as a real, closed set of budget markers - this reuses
#: the same discipline as a genuine EXTRACTION rather than only a
#: disclosure) followed by a real number - never a capability/intent
#: classifier, purely a deterministic number-extraction backstop for
#: when the model's OWN `shopping_state.budget_amount` field came back
#: empty on a turn whose own words plainly state one. Live-reproduced:
#: a compound turn ("show me running shoes under 5000 and compare the
#: best two") had its attention split across search+comparison+budget
#: in one model call, and the budget number was dropped - the discovery
#: hard-filter and the secondary comparison both then had nothing to
#: filter against, letting an over-budget product (Featherline, 6299)
#: through into "the best two."
#: A CONSTRAINT phrasing - "under/below/up to X" - the number is a ceiling
#: on something else being searched for or compared.
_BUDGET_CONSTRAINT_PATTERN = re.compile(
    r"(?:under|below|less\s+than|up\s+to|within|max(?:imum)?|no\s+more\s+than)\s*"
    r"(?:rs\.?|inr|₹|\$|usd)?\s*"
    r"(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
#: A direct STATEMENT phrasing - "my budget is X"/"budget of X"/"I have X
#: to spend" - live-reproduced gap the constraint pattern above cannot
#: cover at all (no "under"/"below"/etc. word present - the whole message
#: IS the budget statement, with nothing else to search for). Reuses the
#: same closed-class budget-word set `_BUDGET_WORDS` (reference_
#: resolution.py) already treats as real budget markers elsewhere in this
#: codebase.
_BUDGET_STATEMENT_PATTERN = re.compile(
    r"budget\s*(?:is\s+)?(?:of\s+)?(?:around\s+)?(?:about\s+)?"
    r"(?:rs\.?|inr|₹|\$|usd)?\s*"
    r"(\d[\d,]*(?:\.\d+)?)"
    r"|(?:spend|spending)\s*(?:is\s+)?(?:around\s+)?(?:about\s+)?"
    r"(?:rs\.?|inr|₹|\$|usd)?\s*"
    r"(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)


def _extract_budget_amount(message: str) -> str | None:
    match = _BUDGET_CONSTRAINT_PATTERN.search(message)
    raw_group: str | None = match.group(1) if match else None
    if raw_group is None:
        match = _BUDGET_STATEMENT_PATTERN.search(message)
        if match:
            raw_group = match.group(1) or match.group(2)
    if raw_group is None:
        return None
    raw = raw_group.replace(",", "")
    try:
        float(raw)
    except ValueError:
        return None
    return raw


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
    priorities_remove: list[str] = Field(default_factory=list)
    mission_status: str | None = None
    #: What to retrieve on this turn, if anything. Falls back to the
    #: shopper's raw message when the model left this blank - the same
    #: "rescue search" behaviour V1's execution layer already uses (see
    #: engine/execution/relevance.py's own module docstring).
    search_query: str | None = None


class UnderstandingResult(BaseModel):
    model_config = {"arbitrary_types_allowed": True}

    delta: StateDelta
    used_model: bool
    #: True only when the model was reachable AND called the tool - lets a
    #: caller distinguish "no delta because nothing changed" from "no delta
    #: because the model was unavailable", without exposing any model text.
    model_name: str | None = None
    #: No source anymore (see module docstring) - always `None`.
    hinted_action_type: str | None = None
    hinted_product_id: str | None = None
    hinted_order_id: str | None = None
    hinted_quantity: int | None = None
    #: "shopping" or "conversational" - derived from `capability ==
    #: "conversational"` (see `InterpretationResult.message_intent`'s own
    #: docstring), not a second, independently-fallible field.
    message_intent: str | None = None
    #: Always `None` now (see module docstring) - kept as a field so
    #: `conversational.py::run_conversational_turn`'s existing signature
    #: needs no change; its own deterministic fallback always answers now.
    conversational_reply: str | None = None
    #: Durable facts THIS SAME call already flagged
    #: (`InterpretationResult.durable_fact_candidates`, normalized to
    #: key/value strings here). The caller (graph.py's `node_understand`)
    #: does the actual signed-in-shopper-only persist, mirroring V1's
    #: chat.py exactly.
    durable_facts: list[dict] = Field(default_factory=list)
    #: Production architecture (2026-09), Phase 1 reconciliation: the raw
    #: `InterpretationResult` this SAME call produced - `graph.py::
    #: node_understand` calls `verification.py::verify()` against it
    #: immediately (real `ConversationContext`/products are already
    #: available by Stage 1) and stores the result as `state[
    #: "verified_interpretation"]` for every downstream node to consume,
    #: never re-derived by a second, independent call anywhere later in
    #: the turn.
    interpretation_claim: InterpretationResult | None = None


async def understand(
    *,
    reasoning: ReasoningService,
    message: str,
    history: list[str] | None,
    shopping_state: dict | None,
    memory_facts: list[dict] | None,
    context: ConversationContext | None = None,
    active_topic_products: list | None = None,
    last_rendered_products: list | None = None,
    all_fetched_products: list | None = None,
    active_topic_label_override: str | None = None,
    store_categories: list[str] | None = None,
    spelling_hint: str | None = None,
) -> UnderstandingResult:
    """The single authoritative interpretation call for this turn - see
    module docstring. Calls `interpretation.interpret_turn()` (Rev. 2's
    own orchestration/formatting layer, unchanged) instead of `reasoning.
    reason()`; everything below reshapes ONE `InterpretationResult` into
    the `StateDelta`/durable-facts/message_intent shape this function
    already returned, and additionally carries the raw claim through for
    the caller to `verify()`.

    `context`/`active_topic_products`/`last_rendered_products`/
    `all_fetched_products`/`active_topic_label_override` are the same real,
    already-fetched inputs `interpret_turn()` always accepted - gathered by
    the caller (`graph.py::node_understand`, reusing the exact fetch logic
    `node_route_capability` used to run for its own, now-retired,
    conditional attempt). `context=None` (nothing built yet) degrades to
    calling `interpret_turn` with an empty real `ConversationContext` - it
    still runs, since Stage 1 must extract a StateDelta on every turn,
    unconditionally, exactly as it always did.
    """
    # `context` is built by `node_load_context` before this ever runs in a
    # real turn (`graph.py`'s node order), so this is always a real object
    # in practice - `None` is only a defensive fallback for a caller
    # (e.g. a unit test) with no context assembled at all, and degrades
    # honestly (no call attempted) rather than fabricating a placeholder
    # `ConversationContext` (whose `session_id`/`connection_id` this
    # function was never given anyway).
    claim: InterpretationResult | None = None
    if context is not None:
        claim = await interpretation_module.interpret_turn(
            reasoning=reasoning, message=message, context=context,
            active_topic_products=active_topic_products,
            last_rendered_products=last_rendered_products,
            all_fetched_products=all_fetched_products,
            active_topic_label_override=active_topic_label_override,
            current_shopping_state=shopping_state,
            store_categories=store_categories,
            spelling_hint=spelling_hint,
        )
    used_model = claim is not None
    model_name = claim.model_name if claim is not None else None

    raw_state = (claim.shopping_state if claim is not None else None) or {}
    clears = set(raw_state.get("clear_fields") or [])

    model_budget_amount = (
        str(raw_state["budget_amount"]) if raw_state.get("budget_amount") is not None else None
    )
    # Deterministic backstop: only ever fills a gap the model's own field
    # left, never overrides a real value the model DID extract (including
    # a deliberate clear - `clear_budget` below is computed from `clears`
    # exactly as before, unaffected by this).
    backstop_budget_amount = (
        _extract_budget_amount(message) if model_budget_amount is None else None
    )
    delta = StateDelta(
        goal=raw_state.get("goal"),
        clear_goal="goal" in clears,
        budget_amount=model_budget_amount or backstop_budget_amount,
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
        priorities_remove=raw_state.get("priorities_remove") or [],
        mission_status=(
            raw_state["mission_status"]
            if raw_state.get("mission_status") in _VALID_MISSION_STATUS
            else None
        ),
        # Always None: no proposed-action list exists anymore in the
        # merged schema (see module docstring) - `orchestrator.py`/
        # `pipeline.py` already fall back to the shopper's raw message for
        # retrieval when this is unset, exactly as before.
        search_query=None,
    )

    # `message_intent` is now derived from `capability` (a richer, already-
    # decided signal - see `InterpretationResult.message_intent`'s own
    # docstring) rather than a second, independently-fallible field on the
    # SAME call. The prior "internally inconsistent" backstop (message_
    # intent='conversational' but a real goal/constraint was also
    # extracted) is retained: a genuinely new goal/must-have/etc this turn
    # is itself deterministic evidence the turn is not purely
    # conversational, whatever the model's own capability label said.
    message_intent = claim.message_intent if claim is not None else None
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

    # No shopper-facing reply field exists in the merged schema at all
    # (see module docstring, invariant 1) - `conversational.py::
    # run_conversational_turn`'s own deterministic, context-aware fallback
    # (unchanged) is what answers a purely conversational turn now,
    # exactly the same "never a raw pre-fetch reply" discipline
    # `respond.py` already applies to every other capability's synthesis.
    conversational_reply: str | None = None

    # Durable facts: normalized directly from THIS call's own
    # `durable_fact_candidates` - no second, separate backstop call exists
    # anymore (see the retired `_interpretation_durable_fact_backstop`
    # note above).
    durable_facts: list[dict] = []
    if claim is not None:
        for candidate in claim.durable_fact_candidates:
            key = _normalize_fact_key(candidate.subject)
            if not key or not candidate.statement:
                continue
            durable_facts.append({"key": key, "value": candidate.statement})

    # Preference write-path fix (2026-09), Phase 6 - still real and still
    # needed: the model's own `durable_fact_candidates` and `shopping_state.
    # nice_to_have_add`/`priorities_add` are two DIFFERENT fields of the
    # SAME call answering an overlapping question, and can still disagree
    # with each other even though they no longer come from two SEPARATE
    # calls. Never invents a preference: only promotes a fact THIS turn's
    # OWN call already, genuinely extracted, into the one still-empty
    # field a natural preference statement belongs in.
    if durable_facts and not (
        delta.priorities_add or delta.nice_to_have_add or delta.must_have_add
        or delta.constraints_add or delta.exclusions_add
    ):
        # Production architecture (2026-09), Phase 2: never resurrect a
        # value the shopper explicitly cancelled - see
        # `cancelled_preferences`'s own docstring.
        cancelled = set((shopping_state or {}).get("cancelled_preferences") or [])
        delta.nice_to_have_add = [
            f["value"] for f in durable_facts if f.get("value") and f["value"] not in cancelled
        ]

    return UnderstandingResult(
        delta=delta,
        used_model=used_model,
        model_name=model_name,
        hinted_action_type=None,
        hinted_product_id=None,
        hinted_order_id=None,
        hinted_quantity=None,
        message_intent=message_intent,
        conversational_reply=conversational_reply,
        durable_facts=durable_facts,
        interpretation_claim=claim,
    )
