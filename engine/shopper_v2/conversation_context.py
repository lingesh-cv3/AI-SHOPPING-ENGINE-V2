"""ConversationContext - the one authoritative "what's relevant right now"
object for a turn, per the approved Rev. 2 architecture spec (2026-09).

Not a new persistence layer: every field here is EITHER a read of an
already-persisted source (`ShoppingStateV2.focus_stack`/`fulfillment_queue`/
`last_rendered`, `ShopperMemoryFact`, `SessionTurn` history) or a fresh read
of real, live external data (the cart) - this module only ASSEMBLES them
into one place every capability reads from, replacing the several
independent, disagreeing reads (`active_topic_product_ids`,
`paused_topic_product_groups`, `resolve_cart_target`'s own `active`
variable, ...) this project's own diagnosis found responsible for a real
class of bugs: an ordinal/historical/cart-add reference resolving against
the wrong scope because two different capabilities each maintained their
own partial view of "what's in view."

Phase 1 of the Rev. 2 migration: purely additive. `build_context()` is
called every turn but its result is not yet consulted by any decision path
- existing behavior is completely unchanged until later migration phases
wire capability nodes to read from it instead of their own private state.
"""

from __future__ import annotations

import logging

from pydantic import BaseModel, Field

from .state import FulfillmentItem, LastRendered, ShoppingStateV2, Topic

logger = logging.getLogger(__name__)


class CartLineSnapshot(BaseModel):
    product_id: str
    title: str
    quantity: int


class CartSnapshot(BaseModel):
    """A real, freshly-fetched read of the cart - never cached across
    turns, never trusted for anything financial (the actual charge is
    always computed fresh at checkout, exactly as today)."""

    cart_id: str
    lines: list[CartLineSnapshot] = Field(default_factory=list)
    item_count: int = 0


class MemoryFactSnapshot(BaseModel):
    key: str
    value: str
    confidence: float = 1.0


class ConversationContext(BaseModel):
    """Everything a turn needs to know about "what's relevant right now."
    Built fresh every turn by `build_context()`; never mutated in place,
    never independently re-derived by a capability - every capability that
    needs any of this reads it from here, not from its own private scan of
    `ShoppingStateV2`.

    `active_topic`/`paused_topics`/`current_mission` are read views over
    `ShoppingStateV2.focus_stack` - the Focus Stack's own shape is
    unchanged and remains the single source for "which conversation thread
    is this." `last_rendered` is deliberately a SEPARATE field (see
    `state.py::LastRendered`'s own docstring) for "what's literally on
    screen" - the two are never conflated here or by any reader of this
    object.
    """

    session_id: str
    connection_id: str

    active_topic: Topic | None = None
    paused_topics: list[Topic] = Field(default_factory=list)
    current_mission: Topic | None = None

    constraints_summary: dict = Field(default_factory=dict)

    last_rendered: LastRendered | None = None

    history: list[str] = Field(default_factory=list)

    #: Capped read of `ShopperMemoryFact` - see `engine/db/memory_facts.py`'s
    #: own `CONTEXT_LIMIT`. Read-only here; writing happens through the
    #: interpretation/verification pipeline (Phase 7 of the migration), not
    #: through this object.
    durable_facts: list[MemoryFactSnapshot] = Field(default_factory=list)

    cart_snapshot: CartSnapshot | None = None

    fulfillment_queue: list[FulfillmentItem] = Field(default_factory=list)


async def build_context(
    *,
    state: ShoppingStateV2,
    adapter,
    cart_id: str | None,
    history: list[str] | None = None,
    memory_facts: list[dict] | None = None,
) -> ConversationContext:
    """Assembles a `ConversationContext` from already-persisted state plus
    one fresh, real cart read - never invents anything; every field here
    traces back to a real source. Degrades honestly (an empty/`None`
    field) rather than raising, matching this package's existing
    discipline for a cart/adapter read that fails (see
    `intent_resolution.py::resolve_cart_target`'s own identical pattern)."""
    active_topic = next((t for t in state.focus_stack if t.state == "active"), None)
    paused_topics = sorted(
        (t for t in state.focus_stack if t.state == "paused"),
        key=lambda t: t.last_active_at,
        reverse=True,
    )
    current_mission = next((t for t in state.focus_stack if t.kind == "mission"), None)

    # Self-healing legacy bridge - same discipline `state.py`'s own
    # `recent_relevant_product_ids`/`paused_topic_product_groups` already
    # use: Focus Stack topic push depends on a Stage-1 LLM field
    # (`delta.goal`) that live-reproduced testing measured is NOT reliably
    # populated for ordinary "show me X" phrasing (0/8 on a first-ever
    # turn, in one measured run this session) - when `focus_stack` never
    # got a topic at all, the OLDER `recent_product_groups` ledger is
    # still the real, honest record of what was actually shown, and
    # `ConversationContext` must not be blind to it just because the newer
    # mechanism didn't fire. Synthetic topics carry an empty `label`
    # (there is no real label to derive without a product fetch this
    # function deliberately doesn't do) - `verification.py`'s own resume
    # resolution accounts for an unlabeled legacy-bridged topic
    # separately, it is never matched by ordinary label-overlap alone.
    if not active_topic and not paused_topics and state.recent_product_groups:
        paused_topics = [
            Topic(
                topic_id=f"legacy:{i}", kind="discovery", label="",
                product_ids=group, state="paused",
            )
            for i, group in enumerate(reversed(state.recent_product_groups))
            if group
        ]

    cart_snapshot: CartSnapshot | None = None
    if cart_id:
        try:
            cart = await adapter.get_cart(cart_id)
            cart_snapshot = CartSnapshot(
                cart_id=cart_id,
                lines=[
                    CartLineSnapshot(
                        product_id=line.product_id, title=line.title, quantity=line.quantity,
                    )
                    for line in cart.lines
                ],
                item_count=sum(line.quantity for line in cart.lines),
            )
        except Exception:  # noqa: BLE001 - degrade honestly, never raise into the turn
            logger.warning("conversation_context: get_cart failed for %r", cart_id, exc_info=True)
            cart_snapshot = None

    durable_facts = [
        MemoryFactSnapshot(
            key=fact.get("key", ""), value=fact.get("value", ""),
            confidence=fact.get("confidence", 1.0),
        )
        for fact in (memory_facts or [])
        if fact.get("key") and fact.get("value")
    ]

    return ConversationContext(
        session_id=state.session_id,
        connection_id=state.connection_id,
        active_topic=active_topic,
        paused_topics=paused_topics,
        current_mission=current_mission,
        constraints_summary={
            "must_have": list(state.must_have),
            "nice_to_have": list(state.nice_to_have),
            "exclusions": list(state.exclusions),
            "priorities": list(state.priorities),
            "budget_amount": state.budget_amount,
            "budget_currency": state.budget_currency,
        },
        last_rendered=state.last_rendered,
        history=list(history or []),
        durable_facts=durable_facts,
        cart_snapshot=cart_snapshot,
        fulfillment_queue=list(state.fulfillment_queue),
    )
