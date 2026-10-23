"""Rev. 2 architecture (2026-09), Phase 2: the genuine-interpretation
orchestration layer.

`interpret_turn()` assembles the real, grounded data a `ConversationContext`
already carries into the shape `ReasoningService.interpret()` needs, calls
it, and returns the raw `InterpretationResult` - a set of CHECKABLE CLAIMS,
never itself a decision. `engine/shopper_v2/verification.py` (Phase 3)
resolves those claims against real data before anything downstream acts on
them; this module does no resolution itself.

Phase 2 of the migration: this module is built and independently testable,
but not yet wired into `graph.py`'s decision path - no capability reads its
output for a real turn yet. That cutover happens capability-by-capability
starting in Phase 4, once Phase 3's verification exists to check this
module's claims against real data.
"""

from __future__ import annotations

from engine.reasoning.service import InterpretationResult, ReasoningService

from .conversation_context import ConversationContext

#: Rev. 2 architecture (2026-09), Phase 5: the maximum number of
#: interpret/verify rounds for one turn - the first `interpret_turn()`
#: call is round 1, each `escalate_ambiguous_reference()` call is one
#: more. Configurable and evidence-driven (spec §6/§13), not an
#: architectural ceiling declared once and never revisited: this default
#: is a starting hypothesis - Phase 2's own live testing found genuine
#: ambiguity rare and a single narrowed retry sufficient for the cases
#: measured so far. Raise it only from measured evidence (the benchmark
#: in PROGRESS.md's own acceptance criteria) that a real, high-value turn
#: needs a third round rather than falling back to an honest clarifying
#: question - never raised speculatively.
REASONING_BUDGET = 2


def _product_dicts(products: list, ids: list[str] | None = None) -> list[dict]:
    """Real product records -> the plain {title, price} shape the
    interpretation prompt expects - never re-derives ids, only formats
    what's already been fetched.

    When `ids` is given, output follows ITS order, never `products`' own
    order - live-reproduced gap this closes (2026-09): a caller commonly
    assembles `products` from a `set` of ids gathered across several
    topics before one shared fetch, and a `set`'s iteration order has no
    relationship to the real, rendered order. Filtering by `products`'
    own order silently showed the model a SCRAMBLED position 1/2/3..., so
    an ordinal reference ("first and last") was reasoned about against
    the wrong order with complete confidence. `ids` (`LastRendered.
    product_ids`/`Topic.product_ids`) is already real display order by
    construction wherever it was written - this only has to preserve it,
    never re-derive it."""
    if ids is None:
        return [
            {"product_id": p.product_id, "title": p.title, "price": str(p.price) if p.price is not None else None}
            for p in products
        ]
    by_id = {p.product_id: p for p in products}
    return [
        {"product_id": p.product_id, "title": p.title, "price": str(p.price) if p.price is not None else None}
        for p in (by_id[pid] for pid in ids if pid in by_id)
    ]


async def interpret_turn(
    *,
    reasoning: ReasoningService,
    message: str,
    context: ConversationContext,
    active_topic_products: list | None = None,
    last_rendered_products: list | None = None,
    all_fetched_products: list | None = None,
    active_topic_label_override: str | None = None,
    current_shopping_state: dict | None = None,
    store_categories: list[str] | None = None,
    spelling_hint: str | None = None,
) -> InterpretationResult | None:
    """Calls `ReasoningService.interpret()` with the real data already
    assembled in `context` - the "pre-ground before reasoning" step of the
    Rev. 2 flow (§5 of the approved spec) is `node_preground`'s job
    (Phase 4+, once a capability is actually cut over); this function
    accepts already-fetched product lists as its own parameters rather
    than fetching anything itself, so it stays a pure orchestration/
    formatting layer, not a second place that calls an adapter.

    `active_topic_products`/`last_rendered_products` are real `Product`
    objects (already fetched by whichever node is migrated to call this)
    - `None` degrades to an empty list, never a guess.

    `all_fetched_products` is the FULL set the caller fetched this turn
    (active + paused + last-rendered ids combined) - used only to give
    each paused topic a few REAL representative titles in the prompt
    (see `_paused_topic_dict` below); without this, a legacy-bridged
    topic (see `conversation_context.py::build_context`'s own docstring -
    a synthetic topic built from the OLDER ledger when Focus Stack never
    tracked one) has an empty `label` and is otherwise invisible to the
    model's own reasoning, defeating the entire point of surfacing it.

    `active_topic_label_override`, when given, replaces the label shown
    for the active topic even though `context.active_topic` itself is
    `None` - for the SAME legacy-ledger case as `all_fetched_products`'
    own docstring: real, currently-active products exist
    (`active_topic_products` is non-empty) even though no real Focus
    Stack topic tracks them yet. Without this the prompt shows those real
    products under a "(none yet)" heading, which live-reproduced testing
    showed the model reasonably (but wrongly, for this case) reads as
    "nothing is really active" and pushes a new topic instead of
    recognizing a refinement of what's already there."""
    mission_summary = None
    if context.current_mission is not None and context.current_mission.mission_item_ids:
        mission_summary = (
            f"\"{context.current_mission.label}\" - "
            f"{len(context.current_mission.mission_item_ids)} item(s) tracked"
        )
    fetched_by_id = {p.product_id: p for p in (all_fetched_products or [])}

    def _paused_topic_dict(topic) -> dict:
        titles = [fetched_by_id[pid].title for pid in topic.product_ids if pid in fetched_by_id]
        label = topic.label or ", ".join(titles[:3])
        return {"label": label, "product_ids": topic.product_ids}

    return await reasoning.interpret(
        message=message,
        history=context.history,
        durable_facts=[
            {"key": f.key, "value": f.value} for f in context.durable_facts
        ],
        active_topic_label=(
            active_topic_label_override
            if active_topic_label_override is not None
            else (context.active_topic.label if context.active_topic else None)
        ),
        active_topic_products=_product_dicts(
            active_topic_products or [],
            context.active_topic.product_ids if context.active_topic else None,
        ),
        paused_topics=[_paused_topic_dict(t) for t in context.paused_topics],
        last_rendered_products=_product_dicts(
            last_rendered_products or [],
            context.last_rendered.product_ids if context.last_rendered else None,
        ),
        cart_lines=(
            [{"title": l.title, "quantity": l.quantity} for l in context.cart_snapshot.lines]
            if context.cart_snapshot else None
        ),
        mission_summary=mission_summary,
        current_shopping_state=current_shopping_state,
        store_categories=store_categories,
        spelling_hint=spelling_hint,
    )


async def escalate_ambiguous_reference(
    *,
    reasoning: ReasoningService,
    message: str,
    context: ConversationContext,
    candidates: list,
) -> InterpretationResult | None:
    """Rev. 2 architecture (2026-09), Phase 5: ONE bounded re-
    interpretation round (the spec's own §5 ESCALATE step), scoped to
    ONLY the real candidates `verification.py::verify()`'s first pass
    found genuinely ambiguous - never a full re-understanding, never a
    third guess at topic/scope. The candidates are presented as what's
    literally in front of the shopper right now (the same framing
    `last_rendered_products` already carries for an ordinary turn),
    because at this point the topic/scope question is already answered -
    only WHICH of these specific real items is still open.

    The caller (a migrated capability node) is responsible for enforcing
    `REASONING_BUDGET` - this function itself has no loop, no retry of
    its own; it is always exactly one call."""
    return await reasoning.interpret(
        message=message,
        history=context.history,
        durable_facts=[{"key": f.key, "value": f.value} for f in context.durable_facts],
        last_rendered_products=_product_dicts(candidates),
    )
