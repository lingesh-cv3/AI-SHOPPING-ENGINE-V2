"""Multi-item shopping missions: "I need running shoes, socks and a
hydration bottle" tracked and refined as three independent requirements,
not collapsed into one search query.

Reuses every existing verified building block this package already has -
`retrieval.retrieve` (the same thin `adapter.search_products` wrapper
Stage 1 discovery uses) and `evidence.build_evidence` (the same
deterministic hard-filter/relevance-verification pass discovery already
runs, given a per-item `shopping_state`-shaped dict built from that one
item's own constraints/must_have/nice_to_have/exclusions/priorities) - no
new ranking heuristic, no new relevance rule, no second implementation of
either.

Extraction/reference-resolution follows the exact shape
`intent_resolution.py` already established for cart references: a SECOND,
narrow, bounded LLM call (`ReasoningService.interpret_mission_items`)
proposes a structured intent (new item labels; a reference - ordinal/
pronoun/name - to an existing item; what changed for it), and this module
deterministically resolves that reference against the REAL current
`mission_items` list before writing anything to state. The model is never
shown a real `item_id` and never asked to invent one - see
`mission_reference_tool()`'s own docstring for why - so there is no path
for it to fabricate a reference to an item that does not exist.

This module writes only to `ShoppingStateV2.mission_items` /
`engine.db.shopping_state`'s `mission_items` column (additive - see that
column's own docstring in `engine/db/models.py`). It never touches
`active_products`, never calls the Decision Engine, Risk Gate, or
ExecutionService, and never constructs a cart action - a shopper turning a
mission item into a cart line still goes through the existing
`intent_resolution.py`/`cart_proposal.py` path exactly as any other
"add that" turn would, using `item.candidates` the same way
`ShoppingStateV2.active_products` is used today.
"""

from __future__ import annotations

import logging
import re
import uuid
from decimal import Decimal, InvalidOperation

from engine.db import shopping_state as shopping_state_store
from engine.reasoning.service import MissionItemIntent, ReasoningService

from .basket_proposal import run_basket_proposal_turn
from .evidence import build_evidence
from .retrieval import retrieve
from .state import MissionItem, ShoppingStateV2

logger = logging.getLogger(__name__)

_MAX_ITEMS = 8

# Multi-item missions extract constraints as free text only (no dedicated
# budget field on `MissionItemIntent` - see engine/reasoning/service.py).
# Without this, a stated "under 5000" constraint only ever affected
# keyword/title relevance matching, never `mission_ranking.rank_and_filter`'s
# real numeric budget hard-filter, since `item_state_dict["budget_amount"]`
# was unconditionally None - a live-demonstrated bug where a shopper asking
# for items "under 5000" could be shown one that costs more. This parses the
# same "under/below/less than <amount>" shape out of the item's own
# constraint text deterministically, no extra model call, so the existing
# unmodified `rank_and_filter` budget filter actually engages for missions
# too.
_BUDGET_PATTERN = re.compile(
    r"(?:under|below|less than|up to|no more than|max(?:imum)?)\s*\$?\s*"
    r"([\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _extract_budget(constraints: list[str]) -> Decimal | None:
    for text in constraints:
        match = _BUDGET_PATTERN.search(text)
        if not match:
            continue
        try:
            return Decimal(match.group(1).replace(",", ""))
        except InvalidOperation:
            continue
    return None


def _dedup_append(existing: list[str], add: list[str]) -> list[str]:
    out = list(existing)
    seen = {s.strip().lower() for s in out}
    for item in add:
        text = item.strip()
        if not text or text.lower() in seen:
            continue
        out.append(text)
        seen.add(text.lower())
    return out


def _resolve_target(intent: MissionItemIntent, items: list[MissionItem]) -> MissionItem | None:
    """Deterministic resolution, identical discipline to
    `intent_resolution.py::_resolve_reference` - only ever returns an item
    that is genuinely in `items`, never a guess."""
    if intent.reference_type == "ordinal":
        if intent.position is not None and 1 <= intent.position <= len(items):
            return items[intent.position - 1]
        return None

    if intent.reference_type == "pronoun":
        if len(items) == 1:
            return items[0]
        return None

    if intent.reference_type == "name":
        if not intent.named_hint:
            return None
        hint = intent.named_hint.strip().lower()
        if not hint:
            return None
        exact = [i for i in items if i.label.strip().lower() == hint]
        if len(exact) == 1:
            return exact[0]
        contains = [i for i in items if hint in i.label.lower() or i.label.lower() in hint]
        if len(contains) == 1:
            return contains[0]
        return None

    return None


async def apply_mission_message(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    history: list[str] | None,
    state: ShoppingStateV2,
) -> tuple[ShoppingStateV2, bool, tuple[str, ...], bool, dict[str, list[str]]]:
    """Interpret one turn against the current mission items, apply
    whatever it genuinely says, run retrieval only for items that need it,
    and persist. Returns `(state, False, (), False, {})` unchanged when this
    turn carries no mission-item information at all (reasoning
    unavailable, or the interpreter found neither a new item, a resolvable
    reference, nor a basket-proposal request) - the caller should then
    fall through to ordinary single-item handling exactly as before; this
    module never turns a plain discovery/compare turn into mission-item
    bookkeeping by itself.

    The third element is the tuple of `item_id`s this turn actually
    created or refined - computed directly from what this function did,
    never by diffing before/after state snapshots (every `MissionItem` is
    deep-copied before any mutation specifically so the caller's own
    `state.mission_items` is never mutated out from under it - a real,
    live-discovered bug: mutating the dict-derived item objects in place
    silently corrupted the caller's "before" snapshot too, since Python
    holds them by reference, making every before/after diff read as "no
    change" even when a refinement had genuinely just been applied).

    The fourth element is `propose_basket` - true only when the shopper
    explicitly asked to add the tracked items to their cart now AND there
    is at least one tracked item to propose anything for. This function
    itself never proposes anything (see this module's own docstring on
    what it does and does not touch) - it is purely a validated routing
    signal for `basket_proposal.py`, called separately by the orchestrator.

    The fifth element, `item_reasons`, maps `item_id` -> real, grounded
    reasons (`mission_ranking.rank_and_filter`'s own `fit_reasons`, never
    invented) for that item's top verified candidate - populated ONLY for
    an item refined by a genuine trade-off statement this turn
    (`intent.priorities_add` non-empty), so `run_mission_turn` can explain
    WHY the new pick fits rather than just reporting a bare option count.
    Empty for every other kind of turn.
    """
    if reasoning is None or not getattr(reasoning, "enabled", False):
        return state, False, (), False, {}

    current_items = [{"item_id": i.item_id, "label": i.label} for i in state.mission_items]

    try:
        intent = await reasoning.interpret_mission_items(
            message=message, history=history, current_items=current_items,
        )
    except Exception:  # noqa: BLE001 - degrade honestly, never raise into the turn
        logger.exception("missions: interpret_mission_items failed")
        return state, False, (), False, {}

    if intent is None:
        return state, False, (), False, {}

    new_labels = [
        label for label in dict.fromkeys(intent.new_item_labels)
        if not any(label.lower() == i.label.lower() for i in state.mission_items)
    ]
    room = max(_MAX_ITEMS - len(state.mission_items), 0)
    new_labels = new_labels[:room]

    target = _resolve_target(intent, state.mission_items)
    has_refinement = bool(
        intent.constraints_add or intent.must_have_add or intent.nice_to_have_add
        or intent.exclusions_add or intent.priorities_add or intent.item_completed
    )
    propose_basket = bool(intent.propose_basket and state.mission_items)

    if not new_labels and not (target is not None and has_refinement) and not propose_basket:
        # Either nothing usable came back, or a refinement was stated but
        # the reference could not be resolved to a real item - never
        # applied to the wrong item, and never silently dropped into the
        # whole-mission fields either.
        return state, False, (), False, {}

    # Deep copies, never the original objects - see this function's own
    # docstring for the bug this specifically closes.
    existing_items = [i.model_copy(deep=True) for i in state.mission_items]
    items_by_id = {i.item_id: i for i in existing_items}
    new_items: list[MissionItem] = []
    for label in new_labels:
        new_items.append(
            MissionItem(item_id=uuid.uuid4().hex[:12], label=label, search_query=label)
        )

    touched_ids: list[str] = [i.item_id for i in new_items]

    if target is not None and has_refinement:
        live_target = items_by_id[target.item_id]
        live_target.constraints = _dedup_append(live_target.constraints, intent.constraints_add)
        live_target.must_have = _dedup_append(live_target.must_have, intent.must_have_add)
        live_target.nice_to_have = _dedup_append(live_target.nice_to_have, intent.nice_to_have_add)
        live_target.exclusions = _dedup_append(live_target.exclusions, intent.exclusions_add)
        live_target.priorities = _dedup_append(live_target.priorities, intent.priorities_add)
        if intent.item_completed:
            live_target.status = "done"
        elif live_target.status == "exploring" and live_target.candidates:
            live_target.status = "comparing"
        # A genuine refinement invalidates the last search - the next
        # retrieval pass below re-runs for this item specifically, never
        # for items untouched this turn.
        #
        # A budget constraint ("under 5000") is a numeric hard-filter, not a
        # useful keyword - live-discovered: including it verbatim in the
        # search text sent to the adapter's keyword search (e.g. "running
        # shoes under 5000") returns zero candidates, since no product title/
        # description contains "under" or "5000", silently starving the
        # budget filter of anything to ever apply to. Excluded from the
        # query text here; `item.constraints` itself (used for the real
        # hard-filter below via `_extract_budget`/`item_state_dict`) is
        # untouched.
        query_constraints = [
            c for c in live_target.constraints if not _BUDGET_PATTERN.search(c)
        ]
        live_target.search_query = " ".join(
            [live_target.label, *query_constraints, *live_target.must_have]
        ).strip()
        live_target.candidates = []
        touched_ids.append(live_target.item_id)

    # A trade-off statement is a refinement whose ONLY new information is a
    # priority - see tradeoff.py's own docstring for why this is a
    # deliberately different case from an ordinary constraint/must-have
    # refinement: the shopper is owed a real, grounded explanation of why
    # this changed the pick, not just a bare "found N options."
    trade_off_item_id = (
        target.item_id if target is not None and intent.priorities_add else None
    )

    all_items = existing_items + new_items

    # Real retrieval, only for items that need it (new, or just refined) -
    # every other item's candidates are left exactly as they were, never
    # re-fetched on a turn that said nothing about them.
    needs_retrieval = [i for i in all_items if i.item_id in touched_ids and i.status != "done"]
    item_reasons: dict[str, list[str]] = {}
    for item in needs_retrieval:
        query = item.search_query or item.label
        result = await retrieve(adapter, query, limit=20)
        item_state_dict = {
            "goal": item.label,
            "budget_amount": _extract_budget(item.constraints),
            "budget_currency": None,
            "constraints": item.constraints,
            "must_have": item.must_have,
            "nice_to_have": item.nice_to_have,
            "exclusions": item.exclusions,
            "priorities": item.priorities,
            "active_products": [],
            "mission_status": item.status,
        }
        evidence = build_evidence(
            query=query,
            raw_said=message,
            products=result.products,
            shopping_state=item_state_dict,
            memory_facts=None,
            catalog_sample=None,
            platform_unavailable=result.unavailable,
        )
        item.candidates = [e.product_id for e in evidence.products if e.relevance_verified]
        if item.item_id == trade_off_item_id and item.candidates:
            # The real, grounded reason(s) `rank_and_filter` (called inside
            # `build_evidence`) attached to the first verified candidate -
            # never a reason this module invents itself.
            top_evidence = next(
                e for e in evidence.products if e.product_id == item.candidates[0]
            )
            if top_evidence.evidence_fields:
                item_reasons[item.item_id] = top_evidence.evidence_fields

    await shopping_state_store.merge(
        state.session_id,
        state.connection_id,
        mission_items=[i.model_dump(mode="json") for i in all_items],
    )
    new_state = state.model_copy(update={"mission_items": all_items})
    return new_state, True, tuple(touched_ids), propose_basket, item_reasons


async def run_mission_turn(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    history: list[str] | None,
    state: ShoppingStateV2,
    decision=None,
    gate=None,
    execution=None,
    connection_id: str | None = None,
    session_id: str | None = None,
    cart_id: str | None = None,
):
    """Orchestrator-facing entry point. Returns
    `(reply, products, new_state, handled, proposal_ids)`. `handled=False`
    means this turn carried no mission-item information - the orchestrator
    should fall through to ordinary single-item discovery/capability
    routing unchanged, exactly as if this module did not exist for this
    turn. `proposal_ids` is the real, persisted proposal id for every
    basket item this turn actually proposed (empty for every other kind of
    mission turn) - the caller needs these to let the shopper approve/
    reject each one; a live-discovered gap (found via Playwright) showed
    that without surfacing them, a real proposal could exist server-side
    with no way for a shopper's client to ever act on it.

    The reply is built deterministically from the real, just-updated
    `mission_items` list - never a model guess about what happened, the
    same discipline `cart_proposal.py` already applies to its own replies.
    `products` holds one real, adapter-verified representative product per
    item that has at least one candidate, purely so the frontend has
    something concrete to render per item - never invented, never a
    product outside that item's own verified `candidates`.

    When the interpreter detected an explicit basket-proposal request
    (`propose_basket`) AND `decision`/`gate`/`execution` were supplied by
    the caller, delegates entirely to
    `basket_proposal.run_basket_proposal_turn` - see that module's own
    docstring for the full safety argument. Without those three, degrades
    honestly rather than silently skipping the request.
    """
    before_ids = {i.item_id for i in state.mission_items}
    new_state, handled, touched_ids, propose_basket, item_reasons = await apply_mission_message(
        reasoning=reasoning, adapter=adapter, message=message, history=history, state=state,
    )
    if not handled:
        return None, [], state, False, []

    if propose_basket:
        if decision is None or gate is None or execution is None or not connection_id or not session_id:
            return (
                "I can't propose a basket through this conversation path right "
                "now - please add items one at a time.",
                [],
                new_state,
                True,
                [],
            )
        reply, products, basket_state, results = await run_basket_proposal_turn(
            decision=decision,
            gate=gate,
            execution=execution,
            adapter=adapter,
            connection_id=connection_id,
            session_id=session_id,
            cart_id=cart_id,
            message=message,
            state=new_state,
        )
        # The real ids of every proposal this turn actually created - see
        # this function's own docstring on why the caller (`orchestrator.py`)
        # needs these: a live-discovered gap found via Playwright showed
        # that without surfacing them, a shopper's client has no way to
        # let them approve/reject a basket-proposed item at all, even
        # though the real, persisted proposal exists server-side.
        proposal_ids = [
            r.proposal["proposal_id"]
            for r in results
            if r.outcome == "proposed" and r.proposal
        ]
        return reply, products, basket_state, True, proposal_ids

    items_by_id = {i.item_id: i for i in new_state.mission_items}
    new_items = [items_by_id[tid] for tid in touched_ids if tid not in before_ids]
    touched = [items_by_id[tid] for tid in touched_ids if tid in items_by_id]

    lines: list[str] = []
    if new_items:
        labels = ", ".join(i.label for i in new_items)
        lines.append(f"Got it - I'm now tracking: {labels}.")
    for item in touched:
        if not item.candidates:
            lines.append(f"I couldn't find anything matching for {item.label} yet.")
        elif item.status == "done":
            lines.append(f"Noted - {item.label} is settled.")
        elif item.item_id in item_reasons:
            # A genuine trade-off statement - explain the real, grounded
            # reason behind the new pick, never just an option count.
            why = "; ".join(item_reasons[item.item_id][:2])
            lines.append(f"For {item.label}, that now fits best - {why}.")
        else:
            count = len(item.candidates)
            lines.append(
                f"For {item.label}, I found {count} option{'s' if count != 1 else ''} "
                "that fit what you've said so far."
            )

    products = []
    for item in touched:
        if not item.candidates:
            continue
        try:
            products.append(await adapter.get_product(item.candidates[0]))
        except Exception:  # noqa: BLE001
            logger.warning("missions: get_product failed for candidate %r", item.candidates[0], exc_info=True)

    reply = " ".join(lines) if lines else "Noted."
    return reply, products, new_state, True, []
