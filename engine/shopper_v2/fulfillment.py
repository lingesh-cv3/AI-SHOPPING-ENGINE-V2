"""Conversational multi-product cart fulfillment (Shopper item 3).

A "add both"/"add these three" cart-intent turn resolves (via
`intent_resolution.py::resolve_cart_target`) to N real, already-verified
product ids - `CartTargetResult.product_ids`. This module owns the
resulting conversation: for each real product, in order,

1. fetch it (real, adapter-verified - never invented),
2. ask ONLY when it actually needs an option (see `_required_choice` -
   derived from the product's own real `ProductVariant.attributes`, never a
   fixed "size" assumption, so a product needing color, flavor, a pack
   size, several attributes at once, or nothing at all is each handled
   correctly without a single line of category-specific code),
3. once a choice is made for THIS item (a button tap - `selected_variant_id`
   - or an exact-text match against a real option label; see
   `_match_answer`), that tap is the shopper's own explicit authorization
   to add THIS item, right now - not a proposal that then sits waiting for
   a second, separate confirmation somewhere else. It is added directly,
   through `engine.execution.trusted_action.run_trusted_action`
   (`ActionType.ADD_TO_CART`) - the SAME Decision Engine -> Risk Gate ->
   `ExecutionService.execute_case` pipeline every other cart action in this
   codebase already runs through (see that module's own docstring for why
   this is not a weaker path than `PROPOSE_CART_CHANGE`, only a more direct
   one for a moment that already IS an explicit shopper action). The Risk
   Gate can still hold an item back for merchant approval
   (`status="awaiting_approval"`) - this never bypasses risk classification,
   it only removes the REDUNDANT extra click for the common case the gate
   already clears automatically.
4. immediately continue to the next item's own question, in the SAME
   conversation, until every item is settled (added, awaiting approval, or
   - honestly - failed), then report the whole set once.

State lives in `ShoppingStateV2.fulfillment_queue` (`state.py`'s
`FulfillmentItem`) - the same additive, replaced-wholesale-on-change
persistence discipline `mission_items` already uses, reused rather than
reinvented.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from engine.db import shopping_state as shopping_state_store
from engine.decision import DecisionEngine
from engine.execution import ExecutionService
from engine.execution.trusted_action import run_trusted_action
from engine.risk import RiskGate
from shared.models import ActionType

from .state import FulfillmentItem, ShoppingStateV2

logger = logging.getLogger(__name__)

_RATIONALE = "shopper chose this while adding multiple products in one conversation"


def _required_choice(product) -> tuple[str, list] | None:
    """Real, closed-form derivation of "does this product need a shopper
    choice, and along what real dimension(s)" - purely from
    `ProductVariant.attributes` (`shared/models/commerce.py`), never a
    fixed vocabulary like "size". Returns `(dimension_label, buyable
    variants)` only when there is a REAL choice to make (2+ distinct,
    currently-buyable variants) - `None` for no variants at all, or for
    exactly one buyable variant (nothing to ask, see the caller)."""
    buyable = [v for v in product.variants if str(v.availability) != "OUT_OF_STOCK"]
    if len(buyable) <= 1:
        return None
    keys: list[str] = []
    for v in buyable:
        for k in v.attributes:
            if k not in keys:
                keys.append(k)
    label = " / ".join(keys) if keys else "option"
    return label, buyable


def _option_payload(v) -> dict:
    label = v.title or (
        ", ".join(f"{k}: {val}" for k, val in v.attributes.items()) or v.variant_id
    )
    return {"variant_id": v.variant_id, "label": label, "left": v.quantity_available}


def _ask_for_item(item: FulfillmentItem) -> str:
    options_text = ", ".join(o["label"] for o in item.options)
    return (
        f"Sure - what {item.dimension_label} would you like for the {item.title}? "
        f"Options: {options_text}."
    )


def _match_answer(
    message: str, options: list[dict], selected_variant_id: str | None
) -> str | None:
    """A real, trusted choice only - never a guess. A `selected_variant_id`
    (a button tap) is trusted only when it names an option genuinely on
    THIS item - never blindly forwarded. Failing that, an exact,
    whole-message, case-insensitive match against a real option's own
    label - the identical restraint `engine/execution/service.py`'s own
    `said`-exact-match already applies for its single-item case (a fuzzy/
    partial match is what produced a real double-add bug there; not
    repeated here)."""
    if selected_variant_id:
        for o in options:
            if o["variant_id"] == selected_variant_id:
                return o["variant_id"]
        return None
    said = message.strip().lower()
    if not said:
        return None
    for o in options:
        if str(o["label"]).strip().lower() == said:
            return o["variant_id"]
    return None


def _tokens(text: str) -> set[str]:
    return {w.strip(".,!?'\"") for w in text.lower().split() if w.strip(".,!?'\"")}


def looks_like_answer_attempt(
    message: str, options: list[dict], selected_variant_id: str | None
) -> bool:
    """Whether `message` is even plausibly trying to answer THIS pending
    question, versus a completely unrelated request that happens to be
    short ("compare both", "what's in my cart?", "add both" - none of
    which name a size/color/flavor at all). Generalized, structural rule:
    a real attempt names at least one word that is actually part of one of
    THIS item's own real option labels ("size", "9", "red", "250g" - real
    catalog data, never invented or category-specific here) - a message
    sharing none of those words is not a rival phrasing of an answer, it
    is a different request entirely, whatever its length.

    Corrected 2026-09-24, replacing a length/phrase-list heuristic
    ("<=6 words, no comma, no 'show me'") that was live-reproduced as
    genuinely wrong on both sides: it trapped "compare both"/"what's in my
    cart?"/"add both" as fake answer attempts (all short, none of them
    naming a size) and re-asked the same question forever, silently
    blocking every other capability in the conversation; a longer genuine
    reference ("what about the shoes from earlier") was separately
    misclassified too. Comparing against the item's own real options
    instead is correct for both cases at once, and generalizes to any
    dimension/vocabulary a real product's variants actually use - never a
    fixed word list.

    A button tap (`selected_variant_id`) is always a real attempt,
    matched against these options or not - a stale/wrong tap is still a
    tap, never a topic change."""
    if selected_variant_id:
        return True
    message_tokens = _tokens(message)
    if not message_tokens:
        return False
    option_tokens: set[str] = set()
    for o in options:
        option_tokens |= _tokens(str(o["label"]))
    return bool(message_tokens & option_tokens)


@dataclass
class _Progress:
    reply: str
    items: list[FulfillmentItem]
    done: bool
    #: Product ids that transitioned to "added" during THIS call only -
    #: never the whole queue's cumulative added set, so a caller emitting
    #: one event per id never double-records an item added on an earlier
    #: turn.
    newly_added: list[str] = field(default_factory=list)


async def _add_item(
    item: FulfillmentItem, variant_id: str | None, *,
    decision, gate, execution, connection_id, session_id, cart_id, message,
) -> None:
    """The one place this module ever mutates a cart - a direct, already-
    risk-gated `ADD_TO_CART`, never a guess: `variant_id` reaching here is
    either `None` (the product genuinely has no choice to make) or a real
    option id the shopper themselves just picked (see `_match_answer`).
    Mutates `item` in place."""
    params = {"chosen_product": item.product_id, "said": message[:80]}
    if variant_id:
        params["chosen_variant"] = variant_id
    executed, _case_id, awaiting = await run_trusted_action(
        decision=decision, gate=gate, execution=execution,
        connection_id=connection_id, session_id=session_id, cart_id=cart_id,
        action_type=ActionType.ADD_TO_CART, parameters=params,
        rationale=f"{_RATIONALE}: {item.title}",
    )
    if awaiting:
        item.status = "awaiting_approval"
        return
    if executed is None or not executed.succeeded:
        item.status = "failed"
        return
    item.status = "added"
    item.chosen_variant_id = variant_id


def _final_reply(items: list[FulfillmentItem]) -> str:
    added = [i for i in items if i.status == "added"]
    awaiting = [i for i in items if i.status == "awaiting_approval"]
    failed = [i for i in items if i.status == "failed"]
    parts = []
    if added:
        names = ", ".join(i.title for i in added)
        parts.append(f"Added to your cart: {names}.")
    if awaiting:
        names = ", ".join(i.title for i in awaiting)
        parts.append(f"{names} needs the shop's approval first - I've passed it on.")
    if failed:
        names = ", ".join(i.title for i in failed)
        parts.append(f"I couldn't add: {names} - please try those separately.")
    return " ".join(parts) if parts else "Nothing left to add."


async def _progress(
    *, items: list[FulfillmentItem], adapter, decision, gate, execution,
    connection_id, session_id, cart_id, message,
) -> _Progress:
    """Advances `items` (in place, real objects) as far as it can go
    WITHOUT a shopper answer it doesn't already have - adding every item
    that needs no real choice straight away, stopping (never guessing) at
    the first item that does, and asking for it."""
    newly_added: list[str] = []
    for item in items:
        if item.status in ("added", "awaiting_approval", "failed"):
            continue
        if item.status == "pending":
            try:
                product = await adapter.get_product(item.product_id)
            except Exception:  # noqa: BLE001 - degrade honestly, never raise into the turn
                logger.warning(
                    "fulfillment: get_product failed for %r", item.product_id, exc_info=True,
                )
                item.status = "failed"
                continue
            item.title = product.title or item.title
            choice = _required_choice(product)
            if choice is None:
                buyable = [
                    v for v in product.variants if str(v.availability) != "OUT_OF_STOCK"
                ]
                if product.variants and not buyable:
                    item.status = "failed"
                    continue
                variant_id = buyable[0].variant_id if buyable else None
                await _add_item(
                    item, variant_id, decision=decision, gate=gate, execution=execution,
                    connection_id=connection_id, session_id=session_id, cart_id=cart_id,
                    message=message,
                )
                if item.status == "added":
                    newly_added.append(item.product_id)
                continue
            label, buyable = choice
            item.status = "needs_variant"
            item.dimension_label = label
            item.options = [_option_payload(v) for v in buyable]
            return _Progress(
                reply=_ask_for_item(item), items=items, done=False, newly_added=newly_added,
            )
        if item.status == "needs_variant":
            return _Progress(
                reply=_ask_for_item(item), items=items, done=False, newly_added=newly_added,
            )
    return _Progress(
        reply=_final_reply(items), items=items, done=True, newly_added=newly_added,
    )


async def _persist(state: ShoppingStateV2, items: list[FulfillmentItem]) -> None:
    state.fulfillment_queue = items
    await shopping_state_store.merge(
        state.session_id, state.connection_id,
        fulfillment_queue=[i.model_dump(mode="json") for i in items],
    )


async def start_fulfillment(
    *,
    adapter,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    cart_id: str | None,
    message: str,
    state: ShoppingStateV2,
    product_ids: list[str],
    quantity: int | None,
) -> tuple[str, list[str]]:
    """Begins a new multi-product fulfillment conversation for
    `product_ids` (2+ real, already-verified ids - the caller,
    `graph.py::node_cart_action`, only ever calls this for a genuine
    plural cart-add reference). Returns (reply, product ids actually
    added so far)."""
    items = [
        FulfillmentItem(product_id=pid, title=pid, quantity=quantity or 1)
        for pid in dict.fromkeys(product_ids)
    ]
    result = await _progress(
        items=items, adapter=adapter, decision=decision, gate=gate, execution=execution,
        connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message,
    )
    await _persist(state, [] if result.done else result.items)
    return result.reply, result.newly_added


async def continue_fulfillment(
    *,
    adapter,
    decision: DecisionEngine,
    gate: RiskGate,
    execution: ExecutionService,
    connection_id: str,
    session_id: str,
    cart_id: str | None,
    message: str,
    state: ShoppingStateV2,
    selected_variant_id: str | None = None,
) -> tuple[str, list[str], bool]:
    """Tries to resolve THIS turn's message as the shopper's answer to the
    one pending fulfillment question (if any). Returns `(reply, product
    ids added this turn, handled)` - `handled=False` means either there was
    no pending question at all, or (live-reproduced fix) this message
    plainly is not an attempt to answer it - a genuinely new request
    ("I need shoes, socks and something for energy") that arrived while an
    old size/color/flavor question from a DIFFERENT, already-abandoned
    add-both conversation was still unanswered. Without this check, a
    pending question that never got answered would hijack every later
    message in the conversation forever, however unrelated - `caller must
    fall through to ordinary routing completely untouched` in both cases;
    the pending item itself is dropped (never left to keep intercepting
    every future turn) rather than silently guessed at."""
    items = list(state.fulfillment_queue)
    pending_idx = next(
        (i for i, it in enumerate(items) if it.status == "needs_variant"), None
    )
    if pending_idx is None:
        return ("", [], False)

    item = items[pending_idx]
    picked = _match_answer(message, item.options, selected_variant_id)
    if picked is None:
        if not looks_like_answer_attempt(message, item.options, selected_variant_id):
            # Abandon honestly - drop the stale queue rather than keep
            # asking a question this new message was never trying to
            # answer. Whatever items were already added this
            # conversation stay added; the unresolved one is simply not
            # pursued further unless the shopper brings it up again.
            await _persist(state, [])
            return ("", [], False)
        # Never guess: the same real question, asked again, honestly.
        return (_ask_for_item(item), [], True)

    await _add_item(
        item, picked, decision=decision, gate=gate, execution=execution,
        connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message,
    )
    just_added = [item.product_id] if item.status == "added" else []
    result = await _progress(
        items=items, adapter=adapter, decision=decision, gate=gate, execution=execution,
        connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message,
    )
    await _persist(state, [] if result.done else result.items)
    return (result.reply, [*just_added, *result.newly_added], True)
