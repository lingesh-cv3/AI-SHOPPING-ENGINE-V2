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
   `_match_answer`), a cart PROPOSAL for exactly that product/variant is
   created (`cart_proposal.run_cart_change_proposal_turn`: Decision Engine ->
   Risk Gate -> stored `PROPOSE_CART_CHANGE`). Contract (Step 25, replacing the
   Step 17 "every add is a proposal" rule for ONE case): when the shopper's own
   words are an explicit command to add (`add_command.is_explicit_add_command`:
   "add it to the cart", never "should I add it?", "don't add it", "sounds
   good", a recommendation or a discovery turn), the proposal is accepted in the
   same turn through `accept_cart_proposal_core` - the very path the Accept
   button and a typed "yes" run (live stock/price re-check, atomic claim,
   Decision -> Risk -> Execution -> Adapter) - and the reply reports what the
   platform ACTUALLY did. Every other add (a model-inferred intent, a
   complement suggestion) is still only a proposal the shopper approves. A
   pending proposal for the same product is reused, never duplicated.
4. immediately continue to the next item's own question, in the SAME
   conversation, until every item is settled (added, awaiting approval, or
   - honestly - failed), then report the whole set once.

State lives in `ShoppingStateV2.fulfillment_queue` (`state.py`'s
`FulfillmentItem`) - the same additive, replaced-wholesale-on-change
persistence discipline `mission_items` already uses, reused rather than
reinvented.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

from engine.db import cart_proposals as cart_proposals_store
from engine.db import shopping_state as shopping_state_store
from engine.decision import DecisionEngine
from engine.execution import ExecutionService
from engine.risk import RiskGate

from .cart_proposal import run_cart_change_proposal_turn
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


#: A deliberately small, closed class of dismissal words - the same
#: "closed linguistic class, not tied to any product domain" discipline
#: `reference_resolution.py`'s own ordinal/quantifier words already use,
#: not an attempt to detect arbitrary intent from open-ended phrasing
#: (that would be the keyword-matching anti-pattern this codebase's own
#: history already rejected for something like cart-ADD detection). A
#: shopper explicitly declining ONE item mid-sequence ("skip this",
#: "never mind that one", "cancel this one", "leave it out", "not this
#: one") is a small, real, closed set of dismissal phrasings - unlike
#: "add X," which has effectively unbounded surface forms.
_SKIP_WORDS = {"skip", "cancel", "nevermind", "never", "leave", "pass", "no"}


def looks_like_skip_request(message: str, options: list[dict]) -> bool:
    """True when `message` is explicitly declining the CURRENT pending
    item, not answering it and not an unrelated interruption. Checked
    BEFORE `looks_like_answer_attempt` in the caller - a message like
    "skip this one" shares no real option-label words with the item's own
    variants, so it would otherwise fall through to "unrelated
    interruption" and silently preserve-without-progressing the queue
    forever, never actually moving past the declined item. Deliberately
    narrow: requires an actual dismissal word AND no option-label word
    (otherwise "skip the 9, give me the 8" - a real answer, not a skip -
    could be misread)."""
    tokens = _tokens(message)
    if not tokens:
        return False
    if tokens & _SKIP_WORDS:
        option_tokens: set[str] = set()
        for o in options:
            option_tokens |= _tokens(str(o["label"]))
        return not (tokens & option_tokens)
    return False


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


#: Product ids this turn actually put in the real cart (set by start/continue_fulfillment). A turn that goes through
#: a path which returns only products (the mission node) reads this to tell the storefront the cart changed.
ADDED_THIS_TURN: contextvars.ContextVar[list[str] | None] = contextvars.ContextVar("fulfillment_added_this_turn", default=None)


def _note_added(ids: list[str]) -> None:
    if ids:
        ADDED_THIS_TURN.set([*(ADDED_THIS_TURN.get() or []), *ids])


_ADD_LOCKS: dict[str, asyncio.Lock] = {}


def _add_lock(connection_id: str, cart_id: str, product_id: str) -> asyncio.Lock:
    """One lock per (merchant, cart, product): a double-click or a retried command must not run two adds. The engine
    is a single process (CLAUDE.md); the real cart is re-read under the lock, so the platform stays the source of
    truth for 'already there'."""
    if len(_ADD_LOCKS) > 2000:  # bounded: drop locks nobody holds (a held/awaited lock is never dropped)
        for key in [k for k, v in _ADD_LOCKS.items() if not v.locked()]:
            _ADD_LOCKS.pop(key, None)
    return _ADD_LOCKS.setdefault(f"{connection_id}|{cart_id}|{product_id}", asyncio.Lock())

#: How long an open size/option question stays answerable.
_QUEUE_TTL = timedelta(minutes=30)


def _question_is_stale(item) -> bool:
    if not item.asked_at:
        return False
    try:
        asked = datetime.fromisoformat(item.asked_at)
    except ValueError:
        return False
    return datetime.now(UTC) - asked > _QUEUE_TTL


async def _propose(item, variant_id, *, decision, gate, execution, connection_id, session_id, cart_id, message,
                   strict: bool = False):
    """Create (or reuse) the PROPOSE_CART_CHANGE record for this item. Returns the proposal dict or None; sets the
    item's status when no proposal could be made. `strict` (an explicit command about to execute): an older pending
    proposal for the same product is reused ONLY if it is for the same variant and quantity the shopper just chose;
    otherwise it is superseded (rejected) and a fresh one made - a stale proposal never executes in place of what the
    shopper just said."""
    try:
        pending = await cart_proposals_store.pending_for_cart(
            connection_id, cart_id, session_id,
        ) if cart_id else []
    except Exception:  # noqa: BLE001 - a lookup failure must not block an honest proposal
        logger.warning("fulfillment: pending proposal lookup failed", exc_info=True)
        pending = []
    existing = next((p for p in pending if p.get("product_id") == item.product_id), None)
    if existing is not None and strict and (
        (existing.get("variant_id") or None) != (variant_id or None)
        or (existing.get("quantity") or 1) != (item.quantity or 1)
    ):
        try:
            await cart_proposals_store.set_state(connection_id, existing["proposal_id"], state="REJECTED")
        except Exception:  # noqa: BLE001 - if it cannot be retired, fall back to the safe path: leave it as the proposal
            logger.warning("fulfillment: could not supersede stale proposal", exc_info=True)
            item.status = "proposed"
            item.proposal_id = existing.get("proposal_id")
            item.chosen_variant_id = variant_id
            return None
        existing = None
    if existing is not None:
        item.status = "proposed"
        item.proposal_id = existing.get("proposal_id")
        item.chosen_variant_id = variant_id
        return existing
    reply, proposal = await run_cart_change_proposal_turn(
        decision=decision, gate=gate, execution=execution,
        connection_id=connection_id, session_id=session_id, cart_id=cart_id,
        message=message, state=ShoppingStateV2(session_id=session_id, connection_id=connection_id),
        operation="add", hinted_product_id=item.product_id, quantity=item.quantity,
        reason=f"{_RATIONALE}: {item.title}", variant_id=variant_id,
    )
    if proposal:
        item.status = "proposed"
        item.proposal_id = proposal.get("proposal_id")
        item.chosen_variant_id = variant_id
        return proposal
    item.status = "awaiting_approval" if "needs to approve" in reply else "failed"
    return None


async def _execute_proposal(item, proposal, *, adapter, connection_id, session_id, cart_id, shopper_id) -> None:
    """Run the proposal through the shared accept core and set the item's status from what ACTUALLY happened.
    Never reports success the platform did not give."""
    from engine.api.chat import accept_cart_proposal_core  # lazy: engine.api imports this package

    try:
        core = await accept_cart_proposal_core(
            connection_id=connection_id, session_id=session_id, cart_id=cart_id,
            proposal_id=proposal["proposal_id"], shopper_id=shopper_id,
        )
    except Exception:  # noqa: BLE001 - 404/409 (already decided, concurrent) or a platform failure: never a raw error
        logger.warning("fulfillment: accept core failed for %r", proposal.get("proposal_id"), exc_info=True)
        # The error may have come AFTER the platform write (a bookkeeping step): the real cart is the source of truth,
        # so say what it holds - never "unchanged" if the product is in fact there, never "added" if it is not.
        held = await _already_in_real_cart(adapter, cart_id)
        item.status = "added" if held and item.product_id in held else "failed"
        item.note = None
        return
    if getattr(core, "cart_changed", False):
        item.status = "added"
        item.note = getattr(core, "reply", None)
    elif getattr(core, "awaiting_person", False):
        item.status = "awaiting_approval"
    else:
        item.status = "failed"


async def _add_item(
    item: FulfillmentItem, variant_id: str | None, *,
    adapter=None, decision, gate, execution, connection_id, session_id, cart_id, message, shopper_id=None,
) -> None:
    """The one place this module acts on a cart. `variant_id` reaching here is either `None` (the product genuinely
    has no choice to make) or a real option id the shopper themselves just picked (see `_match_answer`).

    Not explicit (`item.explicit` False - a model-inferred intent): a PROPOSAL the shopper approves, exactly as
    before. Explicit (the shopper typed an add command): the same proposal is created for the audit trail and then
    accepted in this turn through the shared accept core. Before executing, the REAL cart is re-read under a
    per-product lock - if it is unreadable we do not guess (a retry could double-add) and fall back to the
    proposal; if the product is already there, it is reported, not added again."""
    execute = bool(item.explicit and cart_id and adapter is not None)
    if not execute:
        await _propose(item, variant_id, decision=decision, gate=gate, execution=execution,
                       connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message)
        return
    async with _add_lock(connection_id, cart_id, item.product_id):
        held = await _already_in_real_cart(adapter, cart_id)
        if held is None:
            await _propose(item, variant_id, decision=decision, gate=gate, execution=execution,
                           connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message)
            return
        if item.product_id in held and not item.allow_duplicate:
            item.status = "in_cart"
            return
        proposal = await _propose(item, variant_id, decision=decision, gate=gate, execution=execution,
                                  connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message,
                                  strict=True)
        if proposal is None:
            return
        await _execute_proposal(item, proposal, adapter=adapter, connection_id=connection_id, session_id=session_id,
                                cart_id=cart_id, shopper_id=shopper_id)


def _added_items_summary(added: list[FulfillmentItem]) -> str:
    """"shoe1 - 1999.00 INR, shoe2 - 4299.00 INR, socks - 499.00 INR" plus
    a real total, computed only when every added item reports the SAME
    currency (true for one merchant's own catalog in practice) - never a
    guess or a cross-currency sum; falls back to just naming the items
    (no total) the one time that assumption doesn't hold, or when any
    added item's own price is unknown."""
    lines = [f"{i.title} - {i.price}" if i.price else i.title for i in added]
    amounts: list[Decimal] = []
    currencies: set[str] = set()
    for i in added:
        if not i.price:
            amounts = []
            break
        try:
            amount_str, currency = i.price.rsplit(" ", 1)
            amounts.append(Decimal(amount_str))
            currencies.add(currency)
        except (ValueError, InvalidOperation):
            amounts = []
            break
    if amounts and len(currencies) == 1:
        total = sum(amounts)
        currency = next(iter(currencies))
        return f"As per your order, I've added: {', '.join(lines)}. Total: {total} {currency}."
    return f"Added to your cart: {', '.join(lines)}."


def _final_reply(items: list[FulfillmentItem]) -> str:
    added = [i for i in items if i.status == "added"]
    in_cart = [i for i in items if i.status == "in_cart"]
    proposed = [i for i in items if i.status == "proposed"]
    awaiting = [i for i in items if i.status == "awaiting_approval"]
    failed = [i for i in items if i.status == "failed"]
    skipped = [i for i in items if i.status == "skipped"]
    parts = []
    if added:
        # The platform's own result for a single add ("Added X. Cart now holds N item(s), TOTAL"); a computed
        # summary only when several were added in this sequence.
        parts.append(added[0].note if len(added) == 1 and added[0].note else _added_items_summary(added))
    if in_cart:
        names = ", ".join(i.title for i in in_cart)
        parts.append(f"You already have {names} in your cart.")
    if proposed:
        lines = [f"{i.title} - {i.price}" if i.price else i.title for i in proposed]
        parts.append(
            f"I've prepared {'a proposal' if len(proposed) == 1 else 'proposals'} to add: "
            f"{', '.join(lines)}. Nothing is in your cart until you approve "
            f"{'it' if len(proposed) == 1 else 'each one'}."
        )
    if awaiting:
        names = ", ".join(i.title for i in awaiting)
        parts.append(f"{names} needs the shop's approval first - I've passed it on.")
    if failed:
        names = ", ".join(i.title for i in failed)
        if any(i.explicit for i in failed):
            parts.append(f"I couldn't add {names} to your cart just now - your cart is unchanged.")
        else:
            parts.append(f"I couldn't propose: {names} - please try those separately.")
    if skipped:
        names = ", ".join(i.title for i in skipped)
        parts.append(f"Skipped: {names}.")
    return " ".join(parts) if parts else "Nothing left to add."


async def _progress(
    *, items: list[FulfillmentItem], adapter, decision, gate, execution,
    connection_id, session_id, cart_id, message, shopper_id=None,
) -> _Progress:
    """Advances `items` (in place, real objects) as far as it can go
    WITHOUT a shopper answer it doesn't already have - adding every item
    that needs no real choice straight away, stopping (never guessing) at
    the first item that does, and asking for it."""
    newly_added: list[str] = []
    for item in items:
        if item.status in ("added", "in_cart", "proposed", "awaiting_approval", "failed", "skipped"):
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
            item.price = str(product.price) if product.price is not None else None
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
                    item, variant_id, adapter=adapter, decision=decision, gate=gate, execution=execution,
                    connection_id=connection_id, session_id=session_id, cart_id=cart_id,
                    message=message, shopper_id=shopper_id,
                )
                if item.status == "added":
                    newly_added.append(item.product_id)
                continue
            label, buyable = choice
            item.status = "needs_variant"
            item.asked_at = datetime.now(UTC).isoformat()
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


async def _already_in_real_cart(adapter, cart_id: str | None) -> set[str] | None:
    """Real, currently-in-the-cart product ids - a fresh fetch, never cached, never guessed. An empty set when there
    is no cart yet. `None` when the cart could not be READ: unknown is not empty. A caller that is about to execute
    an add must not treat an unreadable cart as an empty one (a retried command could then add the product twice)."""
    if not cart_id:
        return set()
    try:
        cart = await adapter.get_cart(cart_id)
    except Exception:  # noqa: BLE001 - degrade honestly; the caller decides what unknown means
        logger.warning("fulfillment: get_cart failed for %r", cart_id, exc_info=True)
        return None
    return {line.product_id for line in cart.lines}


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
    explicit: bool = False,
    allow_duplicate: bool = False,
    shopper_id: str | None = None,
) -> tuple[str, list[str]]:
    """Begins a new multi-product fulfillment conversation for
    `product_ids` (1+ real, already-verified ids - single-item callers
    like `missions.py`'s own `add_now` are a legitimate, real call shape
    here too, nothing about this function requires 2+).

    `explicit`: the shopper's own words were an explicit add command
    (`add_command.is_explicit_add_command`) - items are then executed through
    the shared accept core instead of left as proposals (see `_add_item`).
    `allow_duplicate`: the shopper asked for MORE of something already in the
    cart ("add another") - it is then not skipped as already-there.

    Shared precondition, run for EVERY caller: any `product_id` already a
    real line in the actual cart is never re-queued as a fresh "pending"
    item - it is reported honestly as already-there, exactly once, never
    silently duplicated and never silently dropped without mention. When the
    cart cannot be read at all, an explicit command is NOT executed (it falls
    back to proposals): unknown is not empty.

    Returns (reply, product ids actually added so far)."""
    already_in_cart = await _already_in_real_cart(adapter, cart_id)
    cart_unreadable = already_in_cart is None
    already_in_cart = already_in_cart or set()
    unique_ids = list(dict.fromkeys(product_ids))
    if allow_duplicate:
        to_queue, skipped_already_in_cart = unique_ids, []
    else:
        to_queue = [pid for pid in unique_ids if pid not in already_in_cart]
        skipped_already_in_cart = [pid for pid in unique_ids if pid in already_in_cart]

    run_explicit = bool(explicit and not cart_unreadable)
    items = [
        FulfillmentItem(
            product_id=pid, title=pid, quantity=quantity or 1,
            explicit=run_explicit, allow_duplicate=allow_duplicate,
        )
        for pid in to_queue
    ]
    if not items:
        reply = "You already have that in your cart." if len(skipped_already_in_cart) == 1 \
            else "You already have those in your cart."
        return reply, []

    result = await _progress(
        items=items, adapter=adapter, decision=decision, gate=gate, execution=execution,
        connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message,
        shopper_id=shopper_id,
    )
    await _persist(state, [] if result.done else result.items)

    reply = result.reply
    if skipped_already_in_cart:
        # Appended to whichever reply this call actually produces (a
        # variant question or the final summary) - this information is
        # only ever known here, at the START of the sequence
        # (`continue_fulfillment`'s later turns have no way to recover
        # it), so it must not be deferred past this one call. Real
        # titles, fetched fresh - never the bare id shown to a shopper -
        # falling back to the id only if the fetch itself fails.
        titles = []
        for pid in skipped_already_in_cart:
            try:
                titles.append((await adapter.get_product(pid)).title or pid)
            except Exception:  # noqa: BLE001 - degrade to the id, never raise
                titles.append(pid)
        reply = f"{reply} (Already had: {', '.join(titles)}.)"
    _note_added(result.newly_added)
    return reply, result.newly_added


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
    reasoning=None,
    shopper_id: str | None = None,
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
    if item.explicit and _question_is_stale(item):
        # An explicit add whose size question has been open longer than the TTL is not executed on a later, unrelated
        # answer: the queue is dropped and this turn is handled as an ordinary message.
        await _persist(state, [it for it in items if it.status == "added"])
        return ("", [], False)
    picked = _match_answer(message, item.options, selected_variant_id)
    if picked is None:
        if looks_like_skip_request(message, item.options):
            # Explicit decline of THIS ONE item - never re-asked about,
            # never silently re-attempted. The queue survives and
            # advances to whatever is pending after it, same as a real
            # answer would.
            item.status = "skipped"
            result = await _progress(
                items=items, adapter=adapter, decision=decision, gate=gate, execution=execution,
                connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message,
                shopper_id=shopper_id,
            )
            await _persist(state, [] if result.done else result.items)
            _note_added(result.newly_added)
            return (result.reply, result.newly_added, True)
        # Rev. 2 architecture (2026-09), Phase 8: `looks_like_skip_request`
        # is a narrow, closed-class word list ("skip"/"cancel"/"never
        # mind"/etc. exactly) - a real skip phrased differently ("actually
        # forget that one", "leave it out", "don't bother with that")
        # falls through it and used to just abandon (below), silently
        # leaving the item stuck `needs_variant` forever with no further
        # signal to the shopper either way. Consulted ONLY when the
        # deterministic checks are both inconclusive (never replaces
        # them, never runs on the common case) - the same "cheap default,
        # smarter fallback only when needed" cost discipline `verify()`'s
        # own escalation already uses. A genuine model-classified
        # "cancellation" here is treated exactly like an explicit skip.
        if reasoning is not None and getattr(reasoning, "enabled", False):
            try:
                claim = await reasoning.interpret(message=message)
            except Exception:  # noqa: BLE001 - degrade honestly, never fail the turn over this
                logger.warning("fulfillment: conversational-repair check failed", exc_info=True)
                claim = None
            if claim is not None and claim.conversational_repair == "cancellation":
                # Fulfillment-lifecycle fix (2026-09): a general
                # cancellation ("Actually don't add it", "don't add it") is
                # NOT the same shopper intent as `looks_like_skip_request`
                # above (a narrow, closed-class "skip"/"pass"/"next one" -
                # explicitly about THIS ONE item within a batch the
                # shopper still wants the rest of). `conversational_repair
                # == "cancellation"` is the model's own, already-real
                # judgment that the shopper is calling off what they just
                # asked for - live-reproduced, this used to mark only the
                # CURRENT item skipped and silently keep asking about
                # every other still-queued item one at a time, so "don't
                # add it" after a multi-item add produced a confusing
                # string of further size/variant questions for products
                # the shopper never meant to keep adding. A genuine
                # cancellation clears the WHOLE remaining queue - nothing
                # already `"added"` earlier in this same turn sequence is
                # touched (that already reached a real cart write); only
                # what is still pending/needs_variant is called off.
                await _persist(state, [it for it in items if it.status == "added"])
                return ("No problem - I've cancelled that.", [], True)
        if not looks_like_answer_attempt(message, item.options, selected_variant_id):
            # Live-reproduced gap (architecture review): this used to
            # DELETE the whole queue here (`_persist(state, [])`),
            # directly contradicting "an interruption must preserve and
            # resume the correct fulfillment state" - a shopper asking one
            # unrelated aside ("what's your return policy?") mid-sequence
            # permanently lost every item still waiting, not just the one
            # question that went unanswered. The queue is now left exactly
            # as it was - still pending, still resumable - and this turn
            # simply falls through to ordinary routing untouched. The
            # pending item is never silently dropped; only an explicit
            # skip (above) or an actual answer removes it.
            return ("", [], False)
        # Never guess: the same real question, asked again, honestly.
        return (_ask_for_item(item), [], True)

    await _add_item(
        item, picked, adapter=adapter, decision=decision, gate=gate, execution=execution,
        connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message,
        shopper_id=shopper_id,
    )
    just_added = [item.product_id] if item.status == "added" else []
    result = await _progress(
        items=items, adapter=adapter, decision=decision, gate=gate, execution=execution,
        connection_id=connection_id, session_id=session_id, cart_id=cart_id, message=message,
        shopper_id=shopper_id,
    )
    await _persist(state, [] if result.done else result.items)
    _note_added([*just_added, *result.newly_added])
    return (result.reply, [*just_added, *result.newly_added], True)
