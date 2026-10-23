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

TopicKind = Literal["discovery", "mission", "comparison"]
TopicLifecycle = Literal["active", "paused"]

#: How many paused topics the stack retains before the oldest is evicted -
#: matches the scale `_MAX_RECENT_LOOKBACK_GROUPS` already used for the
#: ledger this replaces as a read source (see FocusStack module docs in
#: graph.py/reference_resolution.py). A topic that falls off is not an
#: error case - see `Topic`'s own docstring on honest degradation.
MAX_FOCUS_STACK_DEPTH = 6


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
    #: Guided-mission stage bookkeeping (`guided_mission.py`) - purely
    #: additive, defaults leave every pre-existing item exactly as before.
    #: `guided` marks an item as one stage of an ordered, one-at-a-time
    #: mission; `stage` is where it stands ("pending" = still to choose,
    #: "added" = a product of this stage is in the REAL cart, "skipped" =
    #: the shopper declined it, "unavailable" = nothing in stock to offer).
    #: `added_product_id` is only ever set from a real cart line, never from
    #: a model claim. `sold_out_titles` are real, adapter-reported products
    #: that matched but cannot be bought right now.
    guided: bool = False
    stage: Literal["pending", "added", "skipped", "unavailable"] = "pending"
    added_product_id: str | None = None
    sold_out_titles: list[str] = Field(default_factory=list)


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
    (the add attempt itself failed - never silently dropped) or "skipped"
    (the shopper explicitly declined this one item mid-sequence - "skip
    this", "never mind that one" - a deliberate outcome, distinct from
    "failed," never silently re-attempted or re-asked about again)."""

    product_id: str
    title: str
    quantity: int = 1
    status: Literal[
        "pending", "needs_variant", "added", "in_cart", "proposed", "awaiting_approval", "failed", "skipped"
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
    #: The real product's own price, as a plain "amount currency" string
    #: (`str(Product.price)`), captured the moment `fulfillment.py` fetches
    #: it - never invented, `None` when the adapter reports no price. Used
    #: only to build the final per-item "added: X (price)... total: Y"
    #: summary; never itself trusted for anything financial (the actual
    #: charge is always the real cart/checkout total, computed fresh).
    price: str | None = None
    #: The shopper's own words were an explicit add command (`add_command.is_explicit_add_command`): this item is
    #: executed through the shared accept core instead of left as a proposal. Kept on the item so a later size answer
    #: ("7", a tapped option) completes the SAME command with the same authority - never upgraded by a model.
    explicit: bool = False
    #: "add another": the shopper asked for more of something already in the cart.
    allow_duplicate: bool = False
    #: The platform's own result line for an executed add ("Added X. Cart now holds N item(s), TOTAL").
    note: str | None = None
    #: When the size/option question was asked (ISO, UTC). An explicit command whose question has been open longer
    #: than the fulfillment TTL is dropped, never executed on a much later, unrelated answer.
    asked_at: str | None = None


class Topic(BaseModel):
    """One frame in the Focus Stack - the single authoritative
    representation of "what the shopper is currently talking about, and
    what they were talking about before that, recoverable."

    Replaces three previously-independent, ad-hoc approximations of this
    same concept: `active_products` (single flat list, silently overwritten
    by whichever of ordinary discovery or a mission turn wrote last -
    two DIFFERENT meanings sharing one field), `recent_product_groups` (a
    flat ledger, searched by fuzzy relevance rather than looked up), and
    `mission_items` floating independently at the top level with no
    relationship to whatever else was being discussed. A Topic is now the
    ONE thing any capability consults for "what's in view" - see
    `reference_resolution.py::resolve_reference`, the single resolver every
    capability shares.

    At most one Topic in a FocusStack is ever `state="active"` - the rest
    are `"paused"`, not deleted, and remain independently addressable by a
    later reference or a goal restatement matching their own `label` (see
    `topic_transition.py`'s RESUME rule) until the stack's depth bound
    evicts the oldest of them. An evicted topic is not an error: a later
    reference to it degrades honestly ("I don't have that in view
    anymore"), never a silent mismatch to something else.

    `constraints`/`must_have`/`nice_to_have`/`exclusions`/`priorities` are
    scoped to THIS topic alone - a structural fix, not a clearing rule: a
    brand-new topic starts with these fields empty by construction, so a
    stale preference from an earlier, unrelated topic cannot leak into a
    new request; there is no shared field for it to leak through. Durable
    shopper preferences (`ShopperMemoryFact`/`memory_facts`) are never
    written here - they remain a separate, read-only ranking input exactly
    as they already correctly are (see `mission_ranking.rank_and_filter`'s
    own precedence rule), never temporary topic state.

    `kind="mission"` topics link to their constituent `MissionItem` records
    via `mission_item_ids` rather than duplicating per-item candidate/
    constraint state here - that isolation was already correct; the Topic
    only tracks the mission AS A WHOLE for push/pause/resume purposes.
    `product_ids` for a mission topic is the flattened set of each linked
    item's own top real candidate (mirrors what `graph.py::node_mission`
    already wrote into `active_products` before this model existed - same
    real ids, now owned by an unambiguous frame instead of a dual-purpose
    field)."""

    topic_id: str
    kind: TopicKind
    label: str
    product_ids: list[str] = Field(default_factory=list)
    state: TopicLifecycle = "active"
    mission_item_ids: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    must_have: list[str] = Field(default_factory=list)
    nice_to_have: list[str] = Field(default_factory=list)
    exclusions: list[str] = Field(default_factory=list)
    priorities: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class LastRendered(BaseModel):
    """The exact product ids most recently rendered to the shopper, in
    display order - deliberately separate from `Topic`/`focus_stack`. A
    `Topic` answers "which conversation thread is this"; this answers "what
    is literally on screen right now." Live-reproduced gap this closes
    (2026-09): an ordinal/positional reference ("add first and third",
    "the cheaper one") was being resolved against `active_topic_product_ids`
    - whichever topic the Focus Stack considered active - which is NOT
    necessarily the same list the shopper is actually looking at (the
    active topic can differ from the last-rendered set for a turn or two
    around a topic switch, a historical re-display, or a mission update).
    An ordinal word always means a position in THIS field, never a topic's
    membership.

    Written by whichever capability node just rendered products to the
    shopper (discovery, compare, mission) - exactly once per turn that
    renders anything, always the full, cardinality-capped set actually
    shown, never a superset. Never a decision input on its own: a later
    turn's reference to it still requires a real match here before
    anything resolves, the same "structure decides, never a guess"
    discipline every other field in this module already follows."""

    product_ids: list[str] = Field(default_factory=list)
    source_capability: str | None = None
    source_topic_id: str | None = None
    turn_id: str | None = None
    rendered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


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

    #: The Focus Stack - see `Topic`'s own docstring. Additive: empty for
    #: any session that hasn't been touched by the migrated resolution path
    #: yet, exactly the same "purely additive" discipline `mission_items`/
    #: `fulfillment_queue` were introduced under. `active_products`/
    #: `recent_product_groups` remain real, still-written columns
    #: throughout the migration (V1 still reads/writes `active_products`
    #: directly) - this field does not replace them at the schema level,
    #: only at the V2 resolution-read level once migrated.
    focus_stack: list[Topic] = Field(default_factory=list)

    #: Rev. 2 architecture (2026-09) - see `LastRendered`'s own docstring.
    #: Additive, same discipline as `focus_stack`'s own introduction: `None`
    #: for any session not yet touched by the migrated resolution path.
    last_rendered: LastRendered | None = None

    #: Rev. 2 architecture (2026-09) - see `engine/shopper_v2/answer.py`'s
    #: module docstring. A question-shaped turn that couldn't be fully
    #: resolved this turn, kept so a correction on the NEXT turn is
    #: replayed against the original question instead of treated as an
    #: unrelated new statement. Plain dict (not a typed model) - its only
    #: consumer is the one STATE_CHANGE handler in `graph.py` that reads
    #: and clears it in the same turn; `None` when nothing is pending.
    pending_question: dict | None = None

    #: Production architecture (2026-09), Phase 2 - domain-scoped preference
    #: semantics. Maps each stated preference/priority VALUE (as it appears
    #: in `nice_to_have`/`priorities`/`must_have`) to the domain it was
    #: stated under - the active topic's label (or `goal`) at the moment it
    #: was said, a real, deterministic value, never a fixed enum. Ranking
    #: (`mission_ranking.rank_and_filter`) applies a preference against the
    #: CURRENT retrieval only when this tag is domain-compatible (real word
    #: overlap) with the current query/category, or when no tag is known
    #: for it at all (never silently drop a real preference on an uncertain
    #: classification) - "I prefer lightweight shoes" therefore applies
    #: when shoes are shown again, but does not blindly apply to an
    #: unrelated later request for T-shirts. A value with no entry here
    #: (older sessions, or a genuinely cross-domain statement) is treated as
    #: domain-unrestricted, applying everywhere - the safe default.
    preference_domains: dict[str, str] = Field(default_factory=dict)

    #: Production architecture (2026-09), Phase 2 - cancellation tombstones.
    #: A preference value the shopper explicitly cancelled ("forget the
    #: lightweight preference") is recorded here, not merely removed from
    #: `nice_to_have`/`priorities` - a bare removal cannot by itself stop
    #: the identical value being silently re-derived later from replayed
    #: conversation history (the durable-fact backstop, or the model
    #: re-extracting the same statement from `history` on a later turn).
    #: Checked before any preference add is trusted; cleared the moment the
    #: shopper explicitly restates the SAME value (a genuine re-statement
    #: overrides a tombstone, it does not permanently ban a phrase).
    cancelled_preferences: list[str] = Field(default_factory=list)

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
            "cancelled_preferences": list(self.cancelled_preferences),
            "preference_domains": dict(self.preference_domains),
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
    look, never which ids exist.

    Focus Stack migration: when `state.focus_stack` is populated, it is
    now the SOLE source - the active topic's own `product_ids`, extended
    by paused topics (most-recently-paused first) only when the active
    topic alone doesn't satisfy `minimum`. This is a strict generalization
    of the legacy behavior below (the active topic corresponds to
    `active_products`; paused topics correspond to `recent_product_groups`
    entries), now correctly scoped per-topic rather than one flat list two
    different kinds of turns both wrote into. `active_products`/
    `recent_product_groups` remain the fallback for a session whose Focus
    Stack hasn't been populated yet (mid-migration, or a session that
    predates it), AND for the narrower in-migration case where Focus Stack
    is populated but its own resolution comes up short of `minimum` (see
    the fallback below this docstring, inside the `if state.focus_stack`
    branch) - never a silent override of a real, non-empty Focus Stack
    result, only a shortfall backstop."""
    if state.focus_stack:
        active = next((t for t in state.focus_stack if t.state == "active"), None)
        ids = list(dict.fromkeys(active.product_ids)) if active else []
        if len(ids) >= minimum:
            return ids[:cap]
        paused = sorted(
            (t for t in state.focus_stack if t.state == "paused"),
            key=lambda t: t.last_active_at, reverse=True,
        )
        for topic in paused[:_MAX_RECENT_LOOKBACK_GROUPS]:
            for pid in topic.product_ids:
                if pid not in ids:
                    ids.append(pid)
            if len(ids) >= minimum:
                break
        if len(ids) >= minimum:
            return ids[:cap]
        # State-resolution fix (2026-09), live-reproduced: a topic push is
        # "shape now, content when known" (`topic_transition.py`'s own
        # docstring) - the active topic can be real and genuinely current
        # yet still carry an empty `product_ids` for exactly one turn,
        # between the push and whichever LATER node actually discovers its
        # products (a discovery/mission turn). An `attribute_refinement`/
        # `trade_off` turn is neither of those - it never populates a
        # topic, so a real preference statement landing on that turn found
        # Focus Stack "populated" (one active topic) but empty, and this
        # function's own "Focus Stack is now the SOLE source" rule then
        # refused to look at anything else, even though `active_products`/
        # `last_rendered` still honestly held the real, already-verified
        # ids for what's literally on screen. This is a fallback for a
        # SHORTFALL, never a replacement: Focus Stack is tried FIRST and
        # fully, above; this only fires when it genuinely resolved to
        # fewer than `minimum` real ids, and never invents anything - it
        # reads the exact same already-verified fields the legacy branch
        # below already trusts.
        fallback_ids = list(dict.fromkeys(state.active_products))
        if len(fallback_ids) >= minimum:
            return fallback_ids[:cap]
        if state.last_rendered and state.last_rendered.product_ids:
            rendered_ids = list(dict.fromkeys(state.last_rendered.product_ids))
            if len(rendered_ids) >= minimum:
                return rendered_ids[:cap]
        return ids[:cap]

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


def current_view_product_ids(state: ShoppingStateV2) -> list[str]:
    """The exact set of products the shopper is looking at RIGHT NOW - the
    one and only binding target for a RELATIVE reference ("the first two",
    "both", "these", "that one", "recommend one", "which is better").

    Order of authority: `last_rendered` (what was literally shown on the
    most recent turn that showed products - after a comparison this is the
    compared pair, after a search it is that search's verified results),
    then the active topic's own ids, then the legacy `active_products`.
    Never backfilled from paused topics or older product groups - that is
    `recent_relevant_product_ids`'s job, and only for an EXPLICIT
    cross-topic/historical reference. Root-cause fix (2026-09): relative
    references used to bind through `recent_relevant_product_ids(minimum=
    2)`, which walks into paused topics to reach its minimum - so "compare
    the first two" over a single-result search silently paired that result
    with an unrelated product from an earlier topic."""
    if state.last_rendered and state.last_rendered.product_ids:
        return list(dict.fromkeys(state.last_rendered.product_ids))
    active = active_topic_product_ids(state)
    if active:
        return active
    return list(dict.fromkeys(state.active_products))


def active_topic_product_ids(state: ShoppingStateV2) -> list[str]:
    """The active Focus Stack topic's own real product ids - the
    authoritative "what's currently in view" for reference types (bare
    pronoun/comparative-pair) that inherently mean "from what's in front
    of me right now," never a blend with an earlier, paused topic. Falls
    back to the legacy `active_products` field only when the Focus Stack
    hasn't been populated for this session yet."""
    if state.focus_stack:
        active = next((t for t in state.focus_stack if t.state == "active"), None)
        return list(dict.fromkeys(active.product_ids)) if active else []
    return list(dict.fromkeys(state.active_products))


def paused_topic_product_groups(state: ShoppingStateV2) -> list[list[str]]:
    """Each paused topic's own real product ids, as a group - most-
    recently-paused FIRST (unlike `recent_product_groups`, which is
    oldest-first) - the direct Focus Stack replacement for scanning
    `recent_product_groups` to find "the group the shopper most recently
    stepped away from." Falls back to the legacy ledger (reversed to match
    this same most-recent-first order) only when the Focus Stack hasn't
    been populated for this session yet."""
    if state.focus_stack:
        paused = sorted(
            (t for t in state.focus_stack if t.state == "paused"),
            key=lambda t: t.last_active_at, reverse=True,
        )
        return [list(t.product_ids) for t in paused if t.product_ids]
    return [g for g in reversed(state.recent_product_groups) if g]


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

    # Production architecture (2026-09), Phase 2: domain-tag every
    # newly-stated preference/priority/must-have with the real, current
    # domain it was said under - the active topic's own label (whatever the
    # shopper is currently discussing), falling back to this turn's own
    # `delta.goal` or the session's still-current `goal` when no Focus
    # Stack topic is active yet. A genuine restatement of a previously
    # CANCELLED value clears its tombstone rather than being silently
    # skipped - see `cancelled_preferences`'s own docstring.
    domain = None
    active_topic = next((t for t in current.focus_stack if t.state == "active"), None)
    if active_topic is not None and active_topic.label:
        domain = active_topic.label
    elif delta.goal:
        domain = delta.goal
    elif current.goal:
        domain = current.goal

    newly_stated = [
        *delta.must_have_add, *delta.nice_to_have_add, *delta.priorities_add,
    ]
    preference_domain_updates = (
        {value: domain for value in newly_stated} if domain and newly_stated else None
    )
    cancelled_preferences_remove = (
        [v for v in newly_stated if v in current.cancelled_preferences] or None
    )

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
        priorities_remove=delta.priorities_remove,
        mission_status=delta.mission_status,
        preference_domain_updates=preference_domain_updates,
        cancelled_preferences_remove=cancelled_preferences_remove,
        clear_fields=clear_fields or None,
    )
    new_state = await load(current.session_id, current.connection_id)
    new_state.version = current.version + 1
    return new_state
