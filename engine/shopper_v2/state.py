"""Typed ShoppingStateV2 and its transition semantics.

This is a typed VIEW over the existing `shopping_states` table
(`engine/db/models.py::ShoppingState`, mutated by `engine/db/shopping_state.py`)
- not a new source of truth, not a new table, not a schema change. See this
package's own spec discussion: Stage 1 deliberately reuses the existing
persistence mechanism and its existing per-(session_id, connection_id)
`asyncio.Lock` (in `engine.db.shopping_state`) rather than inventing a second
concurrency mechanism.

`version` is a domain-only, in-memory counter - incremented once per
successful merge within this process's lifetime for this state object. It is
NOT persisted as a database column and is NOT a durable optimistic-
concurrency token: two separate processes (or a restart) do not share it, and
it does not survive past the object holding it. That is a deliberate,
documented limitation, not an oversight - see the "concurrency" note below.
Real persistent versioning (an actual DB column, checked on write) is
explicitly out of scope for Stage 1 and is deferred to whichever later stage
first needs to run at production concurrency; this field exists now so the
`ShoppingStateV2` schema and its response shape don't have to change again
just to add it later.

Concurrency for Stage 1 is handled the same way V1 already handles it: the
existing `engine.db.shopping_state`-owned lock serializes reads and writes
per (session_id, connection_id), so two concurrent Stage 1 turns for the
same session cannot race each other - the same guarantee V1 has always had,
reused rather than reimplemented.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, Field

from engine.db import shopping_state as shopping_state_store

if TYPE_CHECKING:
    from .understanding import StateDelta

MissionStatus = Literal["exploring", "comparing", "deciding", "done"]


class MissionItem(BaseModel):
    """One independent requirement in a multi-item shopping mission ("I need
    running shoes, socks and a hydration bottle" - three of these, each
    tracked and refined independently). See `engine/shopper_v2/missions.py`
    for the extraction/resolution logic that creates and updates these -
    this class is pure data, no behaviour.

    `candidates`/`selected_product_id` hold real, adapter-verified product
    ids only - never a product this item merely mentions. `status` mirrors
    `MissionStatus` at the item level rather than the whole-mission level a
    single-item conversation uses."""

    item_id: str
    label: str
    status: MissionStatus = "exploring"
    constraints: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    #: The search query last used for this item - never the whole mission's
    #: query, so "make the shoes lightweight" only changes what THIS item's
    #: next retrieval looks for.
    search_query: str | None = None
    #: Real, adapter-verified product ids returned for this item's most
    #: recent retrieval - never invented, never carried over from a
    #: different item.
    candidates: list[str] = Field(default_factory=list)
    #: Real product TITLES for `candidates`, same order, same source (the
    #: adapter's own fetch this item's retrieval already did - never a
    #: second lookup, never invented). Exists so a shopper naming a specific
    #: product by name/brand while a mission is active ("add the
    #: Trailblazer") can be resolved to the tracked item it actually
    #: belongs to, structurally - see missions.py's own docstring on the
    #: bug this closes: without real candidate titles, the mission-
    #: reference interpreter is shown only bare labels ("shoes") and has no
    #: way to know a named product belongs to that item, so it invents a
    #: brand-new "ghost" item instead of resolving to the real one.
    candidate_titles: list[str] = Field(default_factory=list)
    selected_product_id: str | None = None


class FulfillmentItem(BaseModel):
    """One product in an in-progress multi-product "add these to my cart"
    conversation (CLAUDE.md Shopper item 3 - conversational multi-product
    purchasing). Tracks, per real product, whatever real variant selection
    it actually needs before it can be proposed - never a fixed "size"
    assumption; see `fulfillment.py::_required_dimension` for how this is
    derived from the product's own real `ProductVariant.attributes`, never
    invented or hardcoded to any one product/category.

    `status` transitions: "pending" (not looked at yet) -> "needs_variant"
    (a real choice is required and not yet made) -> "added" (the shopper's
    own chosen variant, if any, was used to add this real product to the
    real cart directly, via `engine.execution.trusted_action.run_trusted_action`
    - the SAME risk-gated pipeline every other cart action already runs
    through, never a bypass) - or "awaiting_approval" (the Risk Gate itself
    required a human before this specific item could go through - reported
    honestly, never silently retried as if it had succeeded) or "failed"
    (the add attempt itself failed - never silently dropped)."""

    product_id: str
    title: str
    quantity: int = 1
    status: Literal[
        "pending", "needs_variant", "added", "awaiting_approval", "failed"
    ] = "pending"
    #: Real attribute key(s) this product's own buyable variants vary by
    #: (e.g. "size", "color", "size / color", "flavor") - always derived
    #: from `ProductVariant.attributes`, never a fixed vocabulary.
    dimension_label: str | None = None
    #: Real, currently-buyable options - `{"variant_id", "label", "left"}`
    #: per option, the same shape `engine/execution/service.py`'s own
    #: `needs_choice`/`choices` mechanism already uses for the single-item
    #: case - reused, not reinvented.
    options: list[dict] = Field(default_factory=list)
    chosen_variant_id: str | None = None
    proposal_id: str | None = None


class ShoppingStateV2(BaseModel):
    """A typed mirror of one `shopping_states` row, plus a domain-only
    `version` counter. Field names and semantics match
    `engine/db/models.py::ShoppingState` exactly - this is a view, not a
    reinterpretation.
    """

    session_id: str
    connection_id: str

    goal: str | None = None
    budget_amount: str | None = None
    budget_currency: str | None = None

    constraints: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    active_products: list[str] = Field(default_factory=list)
    mission_status: MissionStatus | None = None

    #: Bounded history of recent turns' verified product groups, most
    #: recent LAST (append order) - see `engine/db/models.py::ShoppingState
    #: .recent_product_groups`'s own docstring for why this is additive to
    #: `active_products` rather than a replacement. Populated by
    #: `orchestrator.py` after each discovery turn; consumed by
    #: `intent_resolution.py` so a plural reference spanning a topic switch
    #: ("show shoes" then "show slides" then "add both") can be answered
    #: from what was actually shown, not just the latest turn.
    recent_product_groups: list[list[str]] = Field(default_factory=list)

    #: Multi-item shopping mission tracking - see `MissionItem`'s own
    #: docstring and `engine/shopper_v2/missions.py`. Empty for an ordinary
    #: single-item conversation; the single-item fields above (goal/
    #: constraints/etc.) remain exactly as they were for that case - this is
    #: additive, not a replacement for single-item state.
    mission_items: list[MissionItem] = Field(default_factory=list)

    #: Shopper item 3 - an in-progress multi-product cart-fulfillment
    #: conversation ("add both" -> collect each real product's own real
    #: variant requirement, one at a time, before proposing any of them).
    #: See `FulfillmentItem`'s own docstring and `fulfillment.py`. Empty
    #: for the ordinary single-item/no-variant case - purely additive,
    #: same discipline as `mission_items` above.
    fulfillment_queue: list[FulfillmentItem] = Field(default_factory=list)

    #: Domain-only, in-memory. See module docstring - NOT a DB column, NOT a
    #: durable optimistic-concurrency token.
    version: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def as_prompt_dict(self) -> dict:
        """The shape `engine/reasoning/context.py::build_context` and
        `engine/execution/mission_ranking.py::rank_and_filter` already
        expect - both take a plain `dict`, not a Pydantic model, and this
        keeps this module the only place that knows the translation."""
        return {
            "goal": self.goal,
            "budget_amount": self.budget_amount,
            "budget_currency": self.budget_currency,
            "constraints": list(self.constraints),
            "must_have": list(self.must_have),
            "nice_to_have": list(self.nice_to_have),
            "exclusions": list(self.exclusions),
            "priorities": list(self.priorities),
            "active_products": list(self.active_products),
            "mission_status": self.mission_status,
        }


#: Bound on how many prior ledger groups `recent_relevant_product_ids` will
#: walk back through, and how many ids it will ever return. Matches
#: `graph.py::_LEDGER_MAX_GROUPS` (the ledger's own retention bound) -
#: never a SMALLER, second, arbitrary limit. Context-awareness fix: this
#: used to stop at 2 groups back while `compare.py`'s own historical-match
#: path scanned the full ledger - a shopper switching topics 3+ times
#: ("shoes... socks... a jacket... which one was cheaper, the shoes or the
#: jacket") could ask the SAME historical-recall question through two
#: different capabilities and get two different answers, one honest, one
#: silently truncated at 2. One shared bound now, matched to how far back
#: this codebase actually retains anything at all.
_MAX_RECENT_LOOKBACK_GROUPS = 5
_MAX_RECENT_PRODUCT_IDS = 6


def recent_relevant_product_ids(
    state: ShoppingStateV2, *, minimum: int = 1, cap: int = _MAX_RECENT_PRODUCT_IDS,
) -> list[str]:
    """Real, already-verified product ids currently relevant to the
    conversation - `active_products` (this turn's own single-topic focus)
    extended by prior `recent_product_groups` entries ONLY when the
    current turn's own set doesn't satisfy `minimum` on its own.

    Shared by `compare.py` and `tradeoff.py` (Batch 2 - "resolve from
    current AND historical context"): a real, reported gap - "compare the
    shoes to the slides" after a topic switch could only ever see the
    slides, because `active_products` alone (like `_clarify_with_ledger`'s
    identical problem in `intent_resolution.py`) is deliberately
    overwritten each discovery turn. One shared helper rather than two
    copies of the same lookback logic - see CLAUDE.md's own standing rule
    against duplicating this class of fix per call site.

    Never invents: every id here was already a real, `relevance_verified`
    product from some earlier turn (`orchestrator.py`'s ledger-merge code
    only ever appends verified ids) - this only decides how far back to
    look, never which ids exist."""
    ids = list(dict.fromkeys(state.active_products))
    if len(ids) >= minimum:
        return ids[:cap]
    # `recent_product_groups` is append-ordered, oldest first - its own
    # last entry duplicates `active_products` in the common case (both are
    # written from the same verified_ids at the end of a discovery turn),
    # so walk the REST of it, most-recent-of-the-rest first, bounded to
    # `_MAX_RECENT_LOOKBACK_GROUPS`.
    history = list(reversed(state.recent_product_groups[:-1]))[:_MAX_RECENT_LOOKBACK_GROUPS]
    for group in history:
        for pid in group:
            if pid not in ids:
                ids.append(pid)
        if len(ids) >= minimum:
            break
    return ids[:cap]


async def load(session_id: str, connection_id: str) -> ShoppingStateV2:
    """Read the current mission, or an empty one at version 0 if nothing has
    been captured yet for this (session_id, connection_id) - mirrors
    `engine.db.shopping_state.get`'s own "None means nothing yet" contract,
    just never returning None itself (an empty typed state is a normal,
    representable value; a missing row is not a special case this layer
    needs its caller to branch on)."""
    raw = await shopping_state_store.get(session_id, connection_id)
    if raw is None:
        return ShoppingStateV2(session_id=session_id, connection_id=connection_id)
    return ShoppingStateV2(session_id=session_id, connection_id=connection_id, **raw)


async def apply_delta(current: ShoppingStateV2, delta: "StateDelta") -> ShoppingStateV2:
    """Fold one turn's `StateDelta` into storage via the EXISTING
    `engine.db.shopping_state.merge` - same lock, same read-modify-write
    discipline, same clear/supersede semantics V1 already relies on - then
    re-read and return the new typed state with `version` incremented by
    one. No new persistence path, no new lock, no DB column added."""
    clear_fields = []
    if delta.clear_goal:
        clear_fields.append("goal")
    if delta.clear_budget:
        clear_fields.append("budget_amount")
        clear_fields.append("budget_currency")

    budget_amount: float | None = None
    if delta.budget_amount is not None:
        try:
            budget_amount = float(delta.budget_amount)
        except (TypeError, ValueError):
            budget_amount = None

    await shopping_state_store.merge(
        current.session_id,
        current.connection_id,
        goal=delta.goal,
        budget_amount=budget_amount,
        budget_currency=delta.budget_currency,
        constraints_add=delta.constraints_add,
        constraints_remove=delta.constraints_remove,
        must_have_add=delta.must_have_add,
        must_have_remove=delta.must_have_remove,
        nice_to_have_add=delta.nice_to_have_add,
        nice_to_have_remove=delta.nice_to_have_remove,
        exclusions_add=delta.exclusions_add,
        exclusions_remove=delta.exclusions_remove,
        priorities_add=delta.priorities_add,
        mission_status=delta.mission_status,
        clear_fields=clear_fields or None,
    )
    new_state = await load(current.session_id, current.connection_id)
    new_state.version = current.version + 1
    return new_state
