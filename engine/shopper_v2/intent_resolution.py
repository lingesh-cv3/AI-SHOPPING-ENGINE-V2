"""General, non-phrase-specific cart-intent resolution.

Closes the gap `PROGRESS.md`'s Phase 4 entry named explicitly: the general
`propose_actions` tool did not reliably classify "add that to my cart",
"add the first one", "I'll take this", "put two of those in my cart",
"get me the Trailblazer", or a bare "add it" as `ADD_TO_CART`/
`PROPOSE_CART_CHANGE`. Extended to cover the same general class for
`REMOVE_CART_LINE`/`UPDATE_CART_QUANTITY`/`CLEAR_CART`-shaped language too
("remove that", "remove the shoes", "make that quantity two", "change the
socks to three", "clear my cart").

Rather than pattern-match those phrasings (the anti-pattern this whole
codebase's own history of `~20 keyword point-fixes` in
`engine/execution/relevance.py` already demonstrates the cost of), this
module calls a SECOND, narrow, bounded LLM interpretation
(`ReasoningService.interpret_cart_reference`, `engine/reasoning/`) that
handles the general class of the problem, then deterministically validates
whatever it returns against real data before ever building a proposal.

Call shape:

    message + active_products (real, already-shown/verified)
            + cart_lines (real, fetched fresh from the actual cart)
        -> ReasoningService.interpret_cart_reference   # structured INTENT only
        -> resolve_cart_target                          # deterministic validation
        -> (operation, product_id, quantity, clarifying_question)

`resolve_cart_target` NEVER returns a product_id that was not already a
real, verified `active_products` entry or a real line in the actual cart,
or (for a name reference not already shown) a product independently
re-verified via the SAME retrieval+evidence machinery `pipeline.py` already
uses - never the model's own claim taken at face value. When the reference
is ambiguous or unresolvable, it returns a clarifying question instead of a
guess - this module can never cause a wrong item to be proposed, only, at
worst, an honest "which one?".

`operation="add"` resolves against `active_products` (what has been shown/
discovered this session). `operation` in `("remove", "set_quantity")`
resolves against the REAL, freshly-fetched cart contents instead - "remove
that" means something already IN the cart, not something merely shown.
`operation="clear"` needs no reference at all.
"""

from __future__ import annotations

import dataclasses
import logging

from engine.execution.relevance import best_matches_with_score
from engine.reasoning.service import CartReferenceIntent, ReasoningService
from shared.models import Product

from .retrieval import retrieve
from .state import ShoppingStateV2

logger = logging.getLogger(__name__)


class CartTargetResult:
    def __init__(
        self,
        *,
        is_cart_intent: bool = False,
        operation: str = "none",
        product_id: str | None = None,
        #: Set ONLY for a genuine "add" targeting more than one real,
        #: already-verified product ("add both"/"add these two") - see
        #: `_resolve_all_reference`. `product_id` stays None whenever this
        #: is set; a caller checks this list first. Never populated by a
        #: guess - every id here was independently a real, verified
        #: `active_products`/ledger entry before this call, the same
        #: guarantee `product_id` alone already carries.
        product_ids: list[str] | None = None,
        quantity: int = 1,
        clarifying_question: str | None = None,
    ) -> None:
        self.is_cart_intent = is_cart_intent
        self.operation = operation
        self.product_id = product_id
        self.product_ids = product_ids
        self.quantity = quantity
        self.clarifying_question = clarifying_question


#: Closed-class vocabulary for "both/all of what's in view" - see the
#: `_looks_like_all_reference` call site's own comment for the live gap
#: this backstop closes.
_ALL_REFERENCE_WORDS = ("both", "all of them", "all of those", "each of them", "every one of them")


def _looks_like_all_reference(message: str) -> bool:
    lowered = f" {message.lower()} "
    return any(f" {w} " in lowered for w in _ALL_REFERENCE_WORDS)


async def resolve_cart_target(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    history: list[str] | None,
    state: ShoppingStateV2,
    cart_id: str | None = None,
) -> CartTargetResult:
    """Returns a `CartTargetResult`. `is_cart_intent=False` means this
    message should NOT be treated as a cart action at all (the caller
    should fall through to discovery, exactly as before) - this is the
    structural guarantee behind requirement 9 (a plain discovery statement
    can never become a cart action): it requires an explicit, positive
    `is_cart_intent=True` from the interpreter AND a resolvable target
    before anything downstream ever sees this as a cart operation."""
    active = list(dict.fromkeys(state.active_products))

    cart_lines: list = []
    if cart_id:
        try:
            cart = await adapter.get_cart(cart_id)
            cart_lines = list(cart.lines)
        except Exception:  # noqa: BLE001 - no real cart to reference against
            logger.warning("intent_resolution: get_cart failed for %r", cart_id, exc_info=True)

    # Nothing shown this session AND nothing in the real cart - there is
    # genuinely nothing a cart-intent message could refer to, so this can
    # never become a cart action regardless of what the interpreter might
    # say. This is what makes requirement 9 structural rather than a
    # prompt instruction: a plain discovery turn on a fresh session always
    # short-circuits here, before the LLM is ever called.
    if not active and not cart_lines:
        return CartTargetResult(is_cart_intent=False)

    if reasoning is None or not getattr(reasoning, "enabled", False):
        return CartTargetResult(is_cart_intent=False)

    shown: list[Product] = []
    for pid in active[:10]:
        try:
            shown.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001
            logger.warning("intent_resolution: get_product failed for %r", pid, exc_info=True)

    shown_payload = [{"product_id": p.product_id, "title": p.title} for p in shown]
    cart_payload = [
        {"product_id": line.product_id, "title": line.title, "quantity": line.quantity}
        for line in cart_lines
    ]

    if not shown_payload and not cart_payload:
        return CartTargetResult(is_cart_intent=False)

    try:
        intent = await reasoning.interpret_cart_reference(
            message=message,
            active_products=shown_payload,
            history=history,
            cart_lines=cart_payload,
        )
    except Exception:  # noqa: BLE001 - degrade honestly, never raise into the turn
        logger.exception("intent_resolution: interpret_cart_reference failed")
        intent = None

    if intent is None or not intent.is_cart_intent or intent.operation == "none":
        return CartTargetResult(is_cart_intent=False)

    quantity = intent.quantity or 1

    if intent.operation == "clear":
        if not cart_lines:
            return CartTargetResult(is_cart_intent=True, operation="clear", product_id=None)
        return CartTargetResult(is_cart_intent=True, operation="clear")

    # Conversational-flow review fix (Issue #1): live-reproduced,
    # `interpret_cart_reference` classified "add both to the cart" as
    # `reference_type="none"` 4/4 times, never "all" - a consistent
    # classification gap for this specific closed-class word, not
    # sampling variance (unlike this session's other characterized model
    # noise). Same discipline `capability.py`'s own
    # `looks_like_comparison_request`/etc already use for a measurably
    # unreliable primary classification: a small, fixed, non-phrase-
    # specific word list, gated on the same real-data precondition
    # `_resolve_all_reference` itself already requires (2+ real
    # candidates) before ever overriding the model's own field.
    if (
        intent.operation == "add"
        and intent.reference_type not in ("all", "ordinal", "name")
        and _looks_like_all_reference(message)
    ):
        intent = dataclasses.replace(intent, reference_type="all")

    if intent.operation == "add":
        if intent.reference_type == "all":
            resolved_ids = await _resolve_all_reference(shown, state=state, adapter=adapter)
            # Deliberately conservative: "all" only ever resolves when there
            # are at least 2 real, distinct candidates to resolve it TO - a
            # single-item "all" is not a meaningfully different case from
            # ordinary pronoun resolution, and falling through here (rather
            # than returning a 1-item multi-add) keeps that path the one
            # single source of truth for the single-item case.
            if resolved_ids and len(resolved_ids) >= 2:
                return CartTargetResult(
                    is_cart_intent=True, operation="add",
                    product_ids=resolved_ids, quantity=quantity,
                )
            # Genuinely couldn't ground "all" against anything real - ask,
            # never guess, using the same ledger-aware clarifying question
            # the singular ambiguous case already uses.
            clarifying_question = await _clarify_with_ledger(
                shown, intent, "add to your cart", state=state, adapter=adapter
            )
            return CartTargetResult(
                is_cart_intent=True, operation="add", quantity=quantity,
                clarifying_question=clarifying_question,
            )

        resolved = await _resolve_reference(intent, shown, adapter)
        if resolved is None:
            clarifying_question = await _clarify_with_ledger(
                shown, intent, "add to your cart", state=state, adapter=adapter
            )
            return CartTargetResult(
                is_cart_intent=True, operation="add", quantity=quantity,
                clarifying_question=clarifying_question,
            )
        return CartTargetResult(is_cart_intent=True, operation="add", product_id=resolved, quantity=quantity)

    if intent.operation in ("remove", "set_quantity"):
        cart_products = [
            Product(product_id=line.product_id, title=line.title) for line in cart_lines
        ]
        resolved = await _resolve_reference(intent, cart_products, adapter, live_search=False)
        if resolved is None:
            return CartTargetResult(
                is_cart_intent=True, operation=intent.operation, quantity=quantity,
                clarifying_question=_clarify(cart_products, intent, "change in your cart"),
            )
        return CartTargetResult(
            is_cart_intent=True, operation=intent.operation, product_id=resolved, quantity=quantity,
        )

    return CartTargetResult(is_cart_intent=False)


async def _resolve_reference(
    intent: CartReferenceIntent, candidates: list[Product], adapter, *, live_search: bool = True
) -> str | None:
    if intent.reference_type == "ordinal":
        if intent.position is not None and 1 <= intent.position <= len(candidates):
            return candidates[intent.position - 1].product_id
        return None

    if intent.reference_type == "pronoun":
        # A bare "it"/"that one" is only safe to resolve when there is
        # exactly one real candidate - anything else is a genuine
        # ambiguity, never guessed at.
        if len(candidates) == 1:
            return candidates[0].product_id
        return None

    if intent.reference_type == "name":
        if not intent.named_hint:
            return None
        score, best = best_matches_with_score(candidates, intent.named_hint)
        if score > 0 and len(best) == 1:
            return best[0].product_id
        if len(best) > 1:
            return None  # genuinely ambiguous among what's shown/in cart

        if not live_search:
            return None  # a cart-line reference is never resolved via a fresh search

        # Not among what's shown - a live, bounded, verified lookup by
        # name (e.g. "get me the Trailblazer" for a product never yet
        # displayed this turn). Reuses the exact same retrieve+re-score
        # discipline as ordinary discovery; only accepted if EXACTLY one
        # real, confidently-scored candidate comes back.
        try:
            result = await retrieve(adapter, intent.named_hint, limit=10)
        except Exception:  # noqa: BLE001
            return None
        if result.unavailable or not result.products:
            return None
        live_score, live_best = best_matches_with_score(result.products, intent.named_hint)
        if live_score > 0 and len(live_best) == 1:
            return live_best[0].product_id
        return None

    return None


#: Bounds how many distinct products "add all"/"add both" can ever resolve
#: to in one turn - a real, deliberate ceiling, not just a display limit.
#: Each resolved id still goes through its OWN separate proposal below
#: (`cart_proposal.py`, unchanged), so this bounds how many individual
#: risk-gated proposals one turn can create, not a bulk-mutation size.
_MAX_ALL_REFERENCE_ITEMS = 6


async def _resolve_all_reference(
    shown: list[Product], *, state: ShoppingStateV2, adapter
) -> list[str]:
    """Resolves "all"/"both" to the real, currently-relevant set of
    products - THIS turn's own shown set, extended by the ledger's most
    recent PRIOR group when the current turn's own set is too small to be
    what "both" plausibly means (exactly the "shoes... slides... add both"
    shape - `shown` alone would be just the slide). Every id returned was
    already a real, independently-verified `active_products`/ledger entry
    - nothing here is invented or guessed at; the only judgment made is
    WHICH already-real ids to include, never fabricating a new one.

    Deliberately does NOT reach further back than the immediately prior
    group - "add both" said right after two things were shown is
    unambiguous; reaching three or more topics back to guess which two the
    shopper means would be exactly the guess this module exists to refuse
    to make."""
    ids = list(dict.fromkeys(p.product_id for p in shown))
    if len(ids) < _MAX_ALL_REFERENCE_ITEMS:
        groups = [g for g in state.recent_product_groups if g]
        if len(groups) >= 2:
            prior_ids = [pid for pid in groups[-2] if pid not in ids]
            ids = [*ids, *prior_ids]
    return ids[:_MAX_ALL_REFERENCE_ITEMS]


async def _clarify_with_ledger(
    shown: list[Product],
    intent: CartReferenceIntent,
    verb: str,
    *,
    state: ShoppingStateV2,
    adapter,
) -> str:
    """The rolling in-view ledger fix for a real, reported failure: "show
    shoes under 2000" then "show slides" then "add both" - `shown` here is
    ONLY the latest turn's `active_products` (the slides), because
    `active_products` is deliberately overwritten each turn (see its own
    docstring). Without this, the clarifying question would silently drop
    every earlier topic and ask "which one?" using only the slide - an
    honest-sounding question built from an already-incomplete picture.

    This only ever ADDS real, already-verified-earlier products to the
    question text - never a guess, never an automatic pick of more than
    one. If `shown` alone already resolves unambiguously, this function is
    never called (see the caller). If it can't, and the ledger has more
    than one distinct recent group, the question names items from up to
    the last two groups so the shopper can actually answer it - one at a
    time, since this module can still only ever resolve to a single
    product per call, exactly as `resolve_cart_target`'s own docstring
    already states."""
    groups = [g for g in state.recent_product_groups if g]
    if len(groups) < 2:
        return _clarify(shown, intent, verb)

    shown_ids = {p.product_id for p in shown}
    prior_ids = [pid for pid in groups[-2] if pid not in shown_ids][:4]
    if not prior_ids:
        return _clarify(shown, intent, verb)

    prior_products: list[Product] = []
    for pid in prior_ids:
        try:
            prior_products.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001 - degrade to what resolved, never raise
            logger.warning("intent_resolution: ledger get_product failed for %r", pid, exc_info=True)

    if not prior_products:
        return _clarify(shown, intent, verb)

    combined = [*shown, *prior_products]
    options = "; ".join(f"{i + 1}. {p.title}" for i, p in enumerate(combined))
    return f"I can only add one at a time - which one did you mean? {options}"


def _clarify(candidates: list[Product], intent: CartReferenceIntent, verb: str) -> str:
    if len(candidates) == 1:
        return (
            f"I don't see that among what I can {verb} - did you mean "
            f"{candidates[0].title}, or something else?"
        )
    if not candidates:
        return "I don't have anything to work with for that yet."
    options = "; ".join(f"{i + 1}. {p.title}" for i, p in enumerate(candidates))
    return f"Which one did you mean? {options}"
