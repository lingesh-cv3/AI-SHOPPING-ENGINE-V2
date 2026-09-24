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

from engine.execution import events as shopper_events
from engine.execution import mission_ranking
from engine.execution.relevance import best_matches_with_score

from . import fulfillment
from .basket_proposal import run_basket_proposal_turn
from .evidence import build_evidence, product_as_dict
from .retrieval import retrieve
from .state import MissionItem, ShoppingStateV2

logger = logging.getLogger(__name__)

_MAX_ITEMS = 8

#: Bound on how many of a mission item's own real, already-ranked
#: candidates are ever rendered as product cards for one item, mirroring
#: `mission_ranking.DEFAULT_LIMIT` - the same "display size, not a hard
#: filter" cap that module already uses. Any reply text quoting a count
#: for this item MUST derive it from this same bound (see
#: `_mission_item_products` below) - this is the single place that
#: decides "how many of this item's real matches actually get shown,"
#: so a stated count and a rendered count can never diverge for any
#: mission item, any label, any constraint set.
_MISSION_CANDIDATES_PER_ITEM = 4


async def _mission_item_products(adapter, item: MissionItem) -> list:
    """The real, already-ranked candidates actually rendered for `item` -
    up to `_MISSION_CANDIDATES_PER_ITEM` of them, fetched fresh, skipping
    (never fabricating) any that fail to fetch. Any narrative count for
    this item must be `len()` of this same list - see the caller in
    `run_mission_turn`."""
    products = []
    for pid in item.candidates[:_MISSION_CANDIDATES_PER_ITEM]:
        try:
            products.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001 - skip what can't be fetched, never guess
            logger.warning("missions: get_product failed for candidate %r", pid, exc_info=True)
    return products

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
        # Real-candidate match, structural fix for the "Trailblazer" bug:
        # `named_hint` can also be a REAL product title (or a clear
        # substring of one) this item's own `candidate_titles` already
        # carries - a shopper naming a specific product they were just
        # shown is referring to the item that product belongs to, not
        # introducing something new. Never matched against a DIFFERENT
        # item's candidates, and never against anything not already
        # adapter-verified for this exact item.
        by_candidate = [
            i for i in items
            if any(
                title and (hint in title.lower() or title.lower() in hint)
                for title in i.candidate_titles
            )
        ]
        if len(by_candidate) == 1:
            return by_candidate[0]
        return None

    return None


def _matched_candidate_id(
    named_hint: str | None, item: MissionItem | None, raw_message: str
) -> str | None:
    """The one real candidate product id `named_hint` names, if exactly
    one of `item.candidates` (by its parallel `candidate_titles`) matches -
    same real-data-only discipline as `_resolve_target`'s own candidate
    branch, just returning the specific product rather than the item.

    Never trusts the model's OWN `named_hint` text as proof there was only
    one real match, though - live-reproduced: "get me the road one" against
    candidates "Winter Road Shoe" AND "Starter Road Running Shoe" (both
    genuinely match "road"), the model picked one specific title as
    `named_hint` every time, silently resolving an ambiguity the SHOPPER'S
    OWN WORDS do not resolve - exactly the kind of ungrounded specificity
    `engine/execution/relevance.py`'s `_is_real_title_citation`/
    `weak_single_word_match` already exist to catch for cart references,
    applied here to the same failure mode for mission items. Independently
    re-scores the RAW message (never the model's paraphrase of it) against
    every real candidate title using that same word-overlap discipline -
    only narrows when the raw message's own words AND the model's
    named_hint agree on the same single product.
    """
    if not named_hint or item is None or not item.candidate_titles:
        return None
    hint = named_hint.strip().lower()
    if not hint:
        return None
    hint_matches = [
        pid
        for pid, title in zip(item.candidates, item.candidate_titles)
        if title and (hint in title.lower() or title.lower() in hint)
    ]
    if len(hint_matches) != 1:
        return None

    pairs = [
        (pid, title)
        for pid, title in zip(item.candidates, item.candidate_titles)
        if title
    ]
    top_score, top_matches = best_matches_with_score(
        [{"title": title} for _, title in pairs], raw_message
    )
    if top_score == 0:
        # The raw message had no significant words to score against titles
        # at all (or nothing overlapped) - nothing to independently verify
        # against, so the model's own single hint match stands.
        return hint_matches[0]
    top_titles = {m["title"] for m in top_matches}
    top_ids = {pid for pid, title in pairs if title in top_titles}
    if len(top_ids) != 1 or hint_matches[0] not in top_ids:
        return None
    return hint_matches[0]


async def apply_mission_message(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    history: list[str] | None,
    state: ShoppingStateV2,
    memory_facts: list[dict] | None = None,
) -> tuple[ShoppingStateV2, bool, tuple[str, ...], bool, dict[str, list[str]], str | None]:
    """Interpret one turn against the current mission items, apply
    whatever it genuinely says, run retrieval only for items that need it,
    and persist. Returns `(state, False, (), False, {}, None)` unchanged when this
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

    The sixth element, `add_now_product_id`, is the referenced item's own
    real, already-verified top candidate product id - set ONLY when
    `intent.add_now` was true AND the reference resolved to a real item
    with at least one real candidate, `None` otherwise. This function never
    executes a cart action itself (see this module's own docstring) - the
    caller (`run_mission_turn`) is the one with `decision`/`gate`/
    `execution`/`cart_id` available, and runs this id through the SAME
    trusted `ActionType.ADD_TO_CART` path `fulfillment.py` already uses.
    """
    if reasoning is None or not getattr(reasoning, "enabled", False):
        return state, False, (), False, {}, None

    # `candidate_titles` (real, adapter-verified product names already
    # fetched for this item - see MissionItem's own docstring) rides along
    # so the model can recognize a shopper naming one of THIS item's real
    # candidates by brand/product name, not just its bare label - the
    # structural fix for the "Trailblazer" ghost-item bug, not a per-brand
    # rule. Bounded the same way the shown-products list everywhere else in
    # this package is.
    current_items = [
        {
            "item_id": i.item_id,
            "label": i.label,
            "candidate_titles": i.candidate_titles[:8],
        }
        for i in state.mission_items
    ]

    try:
        intent = await reasoning.interpret_mission_items(
            message=message, history=history, current_items=current_items,
        )
    except Exception:  # noqa: BLE001 - degrade honestly, never raise into the turn
        logger.exception("missions: interpret_mission_items failed")
        return state, False, (), False, {}, None

    if intent is None:
        return state, False, (), False, {}, None

    # Deterministic backstop, defense in depth: even if the model still
    # names something in BOTH new_item_labels and reference_type='name'
    # (an inconsistency reproduced live), never let a label that matches an
    # existing item's own real candidate_titles become a ghost new item -
    # the same discipline this codebase already applies everywhere else
    # (never trust the model's word alone when a deterministic check can
    # verify it against real data).
    def _matches_existing(label: str) -> bool:
        lower = label.lower()
        for i in state.mission_items:
            if lower == i.label.lower():
                return True
            if any(lower in t.lower() or t.lower() in lower for t in i.candidate_titles):
                return True
        return False

    # Manual-test finding (generalized): `interpret_mission_items` is
    # handed BOTH `message` and `history` - a real, live-reproduced defect
    # let it extract a "new item" grounded only in something the shopper
    # said in an EARLIER turn (already present in `history`), not the
    # CURRENT message, turning an unrelated later turn ("add the first and
    # third to my cart") into phantom mission-item bookkeeping for a
    # topic ("running shoes") the shopper had already moved past. This is
    # the same class of "words present only in context, not in what was
    # just said" defect `engine/execution/relevance.py::query_diverges_from_said`
    # already exists to catch for discovery queries, applied here for
    # mission-item creation: a genuinely NEW item must have at least one
    # of its own significant words actually present in THIS turn's raw
    # message, never only in history - never a phrase-specific check, and
    # never a guess when the label is a real word.
    lowered_message = f" {message.lower().strip()} "

    def _grounded_in_current_message(label: str) -> bool:
        words = [w for w in label.lower().split() if len(w) > 2]
        if not words:
            return True  # nothing substantial to check - don't block on it
        return any(w in lowered_message for w in words)

    new_labels = [
        label for label in dict.fromkeys(intent.new_item_labels)
        if not _matches_existing(label) and _grounded_in_current_message(label)
    ]
    room = max(_MAX_ITEMS - len(state.mission_items), 0)
    new_labels = new_labels[:room]

    target = _resolve_target(intent, state.mission_items)
    # A shopper naming one of an item's own real candidates by product name
    # ("add the Trailblazer") is itself a meaningful refinement - narrowing
    # WHICH real, already-verified candidate this item means - even when
    # nothing else about the item changed (no new constraint, no
    # must-have). Resolved deterministically against real candidate_titles
    # only, never a guess - see `_matched_candidate_id` below.
    named_candidate_id = (
        _matched_candidate_id(intent.named_hint, target, message)
        if target is not None and intent.reference_type == "name"
        else None
    )
    has_refinement = bool(
        intent.constraints_add or intent.must_have_add or intent.nice_to_have_add
        or intent.exclusions_add or intent.priorities_add or intent.item_completed
        or named_candidate_id is not None
    )
    propose_basket = bool(intent.propose_basket and state.mission_items)

    if (
        not new_labels
        and not (target is not None and (has_refinement or intent.add_now))
        and not propose_basket
    ):
        # Either nothing usable came back, or a refinement was stated but
        # the reference could not be resolved to a real item - never
        # applied to the wrong item, and never silently dropped into the
        # whole-mission fields either.
        return state, False, (), False, {}, None

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

    if target is not None and named_candidate_id is not None:
        # A named-candidate selection is not a new search - the shopper
        # named one of THIS item's already-verified real candidates, so
        # narrowing to exactly that one (never re-querying, never
        # discarding the other real candidates' own titles' alignment) is
        # the whole fix. Structural counterpart to `basket_proposal.py`'s
        # own "len(candidates) > 1 -> ambiguous" rule: this is what turns a
        # named product into something that rule can actually resolve.
        live_target = items_by_id[target.item_id]
        idx = live_target.candidates.index(named_candidate_id)
        live_target.candidates = [named_candidate_id]
        live_target.candidate_titles = [live_target.candidate_titles[idx]]
        if live_target.status == "exploring":
            live_target.status = "comparing"
        if intent.item_completed:
            live_target.status = "done"
        # Live-reproduced bug: unlike the sibling `elif` branch two lines
        # below, this branch never added `target.item_id` to `touched_ids`
        # - so a real, correctly-applied state change (narrowing to exactly
        # the named candidate) produced zero lines in the reply-building
        # loop below, which falls back to a bare "Noted." for an empty
        # `touched_ids`. The shopper said "add Energy Gel Box", the model
        # correctly resolved it (`named_hint='Energy Gel Box of 12'`), the
        # item WAS narrowed to that exact real candidate - and the shopper
        # was told nothing happened. Every other branch that mutates an
        # item already appends it here; this one silently didn't.
        touched_ids.append(live_target.item_id)
    elif target is not None and intent.add_now and not has_refinement:
        # A bare "add it"/"add the shoes" with no new constraint and no
        # named-candidate narrowing - nothing about the item's own state
        # changes here (no mutation needed), but it must still land in
        # `touched_ids` so the reply-building loop reports on it and
        # `add_now_product_id` (resolved after the retrieval loop below,
        # from whatever this item's own current top real candidate already
        # is) has an item to read from - the same reason the branch above
        # was missing this line.
        touched_ids.append(target.item_id)
    elif target is not None and has_refinement:
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
        # Conversational-flow review fix: `nice_to_have` (a soft preference
        # - "I want something lightweight", never hard-filtered, see
        # mission_ranking.py's own must_have-vs-nice_to_have distinction)
        # never fed into the search query here, only `constraints`/
        # `must_have` did - so a preference could silently fail to narrow
        # the search at all, purely depending on which field the model's
        # own extraction happened to classify it under that turn, an
        # inconsistency with no principled reason behind it. A positive
        # keyword is worth searching on whichever bucket holds it; only
        # `constraints` needs the budget-pattern exclusion above (a number,
        # not a keyword) - `must_have`/`nice_to_have` are always real
        # product-attribute words.
        live_target.search_query = " ".join(
            [live_target.label, *query_constraints, *live_target.must_have, *live_target.nice_to_have]
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
    # re-fetched on a turn that said nothing about them. A named-candidate
    # selection (`target.item_id` when `named_candidate_id is not None`,
    # above) is deliberately excluded even though it is now in
    # `touched_ids` (for the reply-loop fix below) - its own comment
    # already explains why: narrowing to a real, already-verified candidate
    # is not a new search, and re-running retrieval here would silently
    # overwrite the just-narrowed single candidate with a fresh,
    # un-narrowed multi-candidate result, undoing the whole point of the
    # branch that set it.
    named_candidate_item_id = (
        target.item_id if target is not None and named_candidate_id is not None else None
    )
    needs_retrieval = [
        i for i in all_items
        if i.item_id in touched_ids and i.status != "done" and i.item_id != named_candidate_item_id
    ]
    item_reasons: dict[str, list[str]] = {}
    for item in needs_retrieval:
        query = item.search_query or item.label
        result = await retrieve(adapter, query, limit=20)
        # Conversational-flow review fix: `query_diverges_from_said`
        # (relevance.py) exists to catch the MODEL inventing a proxy word
        # never grounded in what the shopper said THIS turn - but a mission
        # item's `search_query` is deliberately built from every real
        # constraint/must_have/nice_to_have accumulated across the item's
        # WHOLE life (see the `search_query` rebuild above), not just this
        # turn's message. Live-reproduced: "For the shoes, I want something
        # lightweight" then, next turn, "Keep the shoes under 5000" -
        # search_query correctly stayed "shoes lightweight", but passing
        # only the CURRENT message as `raw_said` made "lightweight" look
        # like an invented word (absent from THIS turn's text) and zeroed
        # every candidate, even though it was real, shopper-stated
        # constraint text from the immediately preceding turn. `raw_said`
        # must reflect everything this item's own accumulated fields
        # already prove the shopper said, not only the latest sentence.
        raw_said_for_item = " ".join(
            [message, item.label, *item.constraints, *item.must_have, *item.nice_to_have]
        )
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
        # Batch 4 fix (V2 completion audit): this used to always pass
        # `None` here, so a signed-in shopper's real, durable preferences
        # (`prefers_X` facts) were applied to single-item discovery but
        # never to a mission item's own candidates - an inconsistency with
        # no reason behind it, since `rank_and_filter`'s own precedence
        # rule (a THIS-item stated must_have/priority always outranks
        # memory; memory only ever fills a dimension the item said nothing
        # about) already makes this safe per item, not just per
        # conversation. A `prefers_footwear` fact naturally never affects
        # an "energy gel" item's candidates either - it is scored as
        # keyword overlap against THAT item's own real product text, so an
        # irrelevant preference simply never matches anything, the same
        # safety property personalization already has for single-item
        # discovery.
        evidence = build_evidence(
            query=query,
            raw_said=raw_said_for_item,
            products=result.products,
            shopping_state=item_state_dict,
            memory_facts=memory_facts,
            catalog_sample=None,
            platform_unavailable=result.unavailable,
        )
        candidates = [e.product_id for e in evidence.products if e.relevance_verified]
        # Real measurement read-back (see events.rejected_product_ids's own
        # docstring): a candidate this shopper already rejected or removed
        # from cart THIS session is deprioritized, never dropped - it can
        # still be shown, just never as the unprompted top pick again.
        avoid = await shopper_events.rejected_product_ids(state.connection_id, state.session_id)
        item.candidates = shopper_events.deprioritize(candidates, avoid)
        # Real titles, same order, from the SAME adapter fetch above - no
        # second lookup, nothing invented. See MissionItem.candidate_titles'
        # own docstring for what this enables.
        # Same length and order as `item.candidates`, always - an empty
        # string placeholder for a missing title rather than dropping the
        # entry, so the two lists stay index-parallel for any caller (see
        # `_matched_candidate_id`'s `zip` of the two).
        id_to_title = {p.product_id: p.title for p in result.products if p.title}
        item.candidate_titles = [id_to_title.get(pid, "") for pid in item.candidates]
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

    # `add_now` resolved AFTER the retrieval loop above, never before - the
    # named-candidate branch already has its one real candidate by then,
    # but a pure refinement branch cleared `candidates` and only refills it
    # inside that same retrieval loop (`item.candidates` mutated in place,
    # same object `items_by_id` already points at), so resolving any
    # earlier would silently miss the refinement case. Never a guess: only
    # ever the item's own real, already-verified top candidate - the exact
    # same product a shopper reading this turn's reply is looking at,
    # never a second, independent lookup.
    add_now_product_id: str | None = None
    if intent.add_now and target is not None:
        live_target_for_add = items_by_id.get(target.item_id)
        if live_target_for_add is not None and live_target_for_add.candidates:
            add_now_product_id = live_target_for_add.candidates[0]

    return new_state, True, tuple(touched_ids), propose_basket, item_reasons, add_now_product_id


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
    memory_facts: list[dict] | None = None,
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
    (
        new_state, handled, touched_ids, propose_basket, item_reasons, add_now_product_id,
    ) = await apply_mission_message(
        reasoning=reasoning, adapter=adapter, message=message, history=history, state=state,
        memory_facts=memory_facts,
    )
    if not handled:
        return None, [], state, False, []

    if add_now_product_id:
        if decision is None or gate is None or execution is None or not connection_id or not session_id:
            return (
                "I can't add that through this conversation path right now - "
                "please try again from the shop page.",
                [],
                new_state,
                True,
                [],
            )
        # Live-reproduced gap this closes: a shopper naming one already-
        # tracked item ("add Energy Gel Box") had nowhere to go but a
        # narrowing-only reply ("found 1 option") or, before that, a bare
        # "Noted." with nothing added at all. Reuses `fulfillment.py`'s own
        # `start_fulfillment` - the SAME risk-gated `ActionType.ADD_TO_CART`
        # path every other add in this codebase goes through (variant
        # question included, if the real product needs one) - never a
        # second, parallel add implementation. A single real id is a
        # legitimate call shape for `start_fulfillment` even though its own
        # docstring describes its usual multi-item caller; nothing about it
        # requires 2+ items.
        reply, newly_added = await fulfillment.start_fulfillment(
            adapter=adapter, decision=decision, gate=gate, execution=execution,
            connection_id=connection_id, session_id=session_id, cart_id=cart_id,
            message=message, state=new_state, product_ids=[add_now_product_id], quantity=1,
        )
        # Raw `Product` objects, matching the SAME shape the ordinary
        # reply path below returns via `_mission_item_products` - never a
        # dict here, which would silently diverge from what this
        # function's own callers already expect.
        products = []
        for pid in newly_added:
            try:
                products.append(await adapter.get_product(pid))
            except Exception:  # noqa: BLE001 - degrade honestly, never invent a card
                logger.warning("missions: get_product failed for added id %r", pid, exc_info=True)
        return reply, products, new_state, True, []

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
        # Live-reproduced bug: "add both" after "compare both" (itself
        # narrowed to exactly the two products just discussed, via
        # `state.active_products` - see graph.py::node_mission's own
        # ledger-write comment) still saw an item's FULL, unnarrowed
        # candidate list here, so "both" resolved one item unambiguously
        # (it only ever had 1 candidate) and asked "which one?" for the
        # other - even though the immediately relevant context had
        # already picked one specific product for it. Narrows an item's
        # candidates to a single one ONLY when exactly one of its own
        # real, already-verified candidates also appears in
        # `active_products` - the same real ledger `compare.py`/
        # `tradeoff.py` already read, never a guess, never narrowing an
        # item whose ledger overlap is itself ambiguous (0 or 2+ matches).
        active_ids = set(state.active_products)
        if active_ids:
            for item in new_state.mission_items:
                if len(item.candidates) <= 1:
                    continue
                overlap = [
                    (pid, title)
                    for pid, title in zip(item.candidates, item.candidate_titles)
                    if pid in active_ids
                ]
                if len(overlap) == 1:
                    item.candidates = [overlap[0][0]]
                    item.candidate_titles = [overlap[0][1]]
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

    # Fetched ONCE per item, up front - the reply text below and the
    # `products` this function returns both read from this SAME dict, so a
    # stated count and what actually renders can never diverge even if a
    # fetch fails partway through (see `_mission_item_products`'s own
    # docstring and this function's own docstring on the result-count/
    # render-consistency contract this closes).
    rendered_by_item: dict[str, list] = {}
    for item in touched:
        if item.candidates and item.status != "done":
            rendered_by_item[item.item_id] = await _mission_item_products(adapter, item)

    lines: list[str] = []
    if new_items:
        labels = ", ".join(i.label for i in new_items)
        lines.append(f"Got it - I'm now tracking: {labels}.")
    for item in touched:
        if not item.candidates:
            # Conversational-flow review fix (Issue #6): a bare "couldn't
            # find anything" gives the shopper no way to act - re-runs the
            # SAME real search this item's own state already committed to
            # (never a new query) purely to name which real hard filter is
            # actually eliminating everyone and the closest real option
            # that fails only that one, using `explain_no_match`'s own
            # grounded-only discipline. Best-effort: a fetch failure here
            # degrades to the honest generic line, never raises into the
            # turn.
            explanation = None
            try:
                why_result = await retrieve(adapter, item.search_query or item.label, limit=20)
                explanation = mission_ranking.explain_no_match(
                    [product_as_dict(p) for p in why_result.products],
                    {
                        "must_have": item.must_have,
                        "exclusions": item.exclusions,
                        "budget_amount": _extract_budget(item.constraints),
                    },
                )
            except Exception:  # noqa: BLE001 - degrade honestly, never raise to the shopper
                logger.warning("missions: explain_no_match failed for %r", item.item_id, exc_info=True)
            if explanation:
                lines.append(f"I couldn't find anything for {item.label} that fits everything you've said - {explanation}.")
            else:
                lines.append(f"I couldn't find anything matching for {item.label} yet.")
        elif item.status == "done":
            lines.append(f"Noted - {item.label} is settled.")
        elif item.item_id in item_reasons:
            # A genuine trade-off statement - explain the real, grounded
            # reason behind the new pick, never just an option count.
            why = "; ".join(item_reasons[item.item_id][:2])
            lines.append(f"For {item.label}, that now fits best - {why}.")
        else:
            # Result-count/render-contract fix: the number stated here is
            # `len()` of the SAME real, already-fetched list this function
            # returns as `products` for this item - never a separately
            # computed number (the item's full `candidates` count).
            # Live-reproduced, generalized bug: only `candidates[0]` was
            # ever rendered per item, but the text quoted the FULL
            # candidate count - so "2 options" could be said while exactly
            # 1 product card rendered, for any item with more real matches
            # than actually get shown, on any turn, any capability. Also
            # correctly reads as 0 (not 1) if every fetch for this item's
            # own candidates failed - a transient fetch failure degrades
            # the claimed count along with the render, never just the
            # render.
            count = len(rendered_by_item.get(item.item_id, []))
            if count == 0:
                lines.append(f"I couldn't fetch a real match for {item.label} right now - please try again in a moment.")
            else:
                line = (
                    f"For {item.label}, I found {count} option{'s' if count != 1 else ''} "
                    "that fit what you've said so far."
                )
                # Manual-test finding (Issue #4, generalized): a shopper who
                # just narrowed an EXISTING item with a new constraint is
                # owed a reason for what disappeared, not only a shopper
                # who gets zero results - re-runs the same real search this
                # item's own state already committed to (never a new
                # query) purely to compute which stocked, non-surviving
                # candidates existed and why, via the same hard-filter
                # checks `explain_no_match` already uses. Only for an
                # item that existed BEFORE this turn (a brand-new item has
                # no narrowing story yet) and only when there is real,
                # checkable exclusion to report - never invented, never
                # shown for a turn where nothing was actually narrowed out.
                if item not in new_items:
                    try:
                        raw_result = await retrieve(adapter, item.search_query or item.label, limit=20)
                        narrowing = mission_ranking.explain_narrowing(
                            [product_as_dict(p) for p in raw_result.products],
                            set(item.candidates),
                            {
                                "must_have": item.must_have,
                                "exclusions": item.exclusions,
                                "budget_amount": _extract_budget(item.constraints),
                            },
                        )
                    except Exception:  # noqa: BLE001 - degrade honestly, never raise to the shopper
                        narrowing = None
                        logger.warning("missions: explain_narrowing failed for %r", item.item_id, exc_info=True)
                    if narrowing:
                        line += f" ({narrowing}.)"
                lines.append(line)

    products = []
    for item in touched:
        products.extend(rendered_by_item.get(item.item_id, []))

    reply = " ".join(lines) if lines else "Noted."
    return reply, products, new_state, True, []
