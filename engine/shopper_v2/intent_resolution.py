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

`operation="add"` never resolves WHICH product here (Rev. 2 migration,
2026-09) - that decision belongs solely to `verification.py::verify()`,
which sees every topic genuinely in view (`ConversationContext`), not just
this module's own active-topic-only ledger. This module only classifies
whether an "add" was said at all, and asks honestly when reached (meaning
the unified path already declined to resolve it). `operation` in
`("remove", "set_quantity")` still resolves directly here, against the
REAL, freshly-fetched cart contents - "remove that" means something
already IN the cart, a real single source of truth with no topic-scoping
ambiguity to get wrong. `operation="clear"` needs no reference at all.
"""

from __future__ import annotations

import logging

from engine.execution.relevance import best_matches_with_score
from engine.reasoning.service import CartReferenceIntent, ReasoningService
from shared.models import Product

from .state import ShoppingStateV2, active_topic_product_ids, paused_topic_product_groups

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


async def resolve_cart_target(
    *,
    reasoning: ReasoningService,
    adapter,
    message: str,
    history: list[str] | None,
    state: ShoppingStateV2,
    cart_id: str | None = None,
    skip_own_interpretation: bool = False,
) -> CartTargetResult:
    """Returns a `CartTargetResult`. `is_cart_intent=False` means this
    message should NOT be treated as a cart action at all (the caller
    should fall through to discovery, exactly as before) - this is the
    structural guarantee behind requirement 9 (a plain discovery statement
    can never become a cart action): it requires an explicit, positive
    `is_cart_intent=True` from the interpreter AND a resolvable target
    before anything downstream ever sees this as a cart operation.

    `skip_own_interpretation` (production architecture review §3, "no
    cascading LLM fallback"): set by the caller (`node_reference_
    resolution`) when IT already made a real interpretation attempt for
    this exact message this turn and it failed to resolve anything
    actionable - this function must not retry the identical question with
    its own separate `interpret_cart_reference` call in that case. Stays
    `False` (the call proceeds as the genuinely first attempt) whenever no
    attempt was made yet this turn - e.g. a message that isn't an explicit
    cart command ("I'll take this", "get me the Trailblazer"), which this
    function's own call is still the right, first, and only place to
    classify."""
    active = active_topic_product_ids(state)

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

    if skip_own_interpretation:
        # No cascading LLM fallback: an interpretation attempt for this
        # exact message already ran this turn and failed - degrade
        # straight to "not a resolvable cart intent" (the same honest
        # result a raised exception already produces below) rather than
        # trying the identical question a second, independent time.
        intent = None
    else:
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

    if intent.operation == "add":
        # Rev. 2 migration (2026-09): deciding WHICH product an "add"
        # targets is no longer this module's job. That decision is made
        # exactly once, by `verification.py::verify()`, against the real,
        # full `ConversationContext` - every topic genuinely in view this
        # turn (active + every paused topic), not just this function's own
        # `active_topic_product_ids` (`shown` below). This function is
        # only ever reached for "add" after that unified path already
        # declined to resolve cleanly (model unavailable this turn, or a
        # genuine, real ambiguity even the unified path couldn't settle) -
        # it must never independently pick a product here. Doing so was
        # exactly the topic-blind bug this migration closes: an ordinal
        # like "the first shoe" could only ever resolve against whatever
        # happened to be active, regardless of the noun actually used,
        # because a paused topic's own products were structurally invisible
        # to this function. The only honest thing left to do is ask - never
        # guess a second time with a narrower view than the first attempt
        # already had.
        clarifying_question = await _clarify_with_ledger(
            shown, intent, "add to your cart", state=state, adapter=adapter
        )
        return CartTargetResult(
            is_cart_intent=True, operation="add", quantity=quantity,
            clarifying_question=clarifying_question,
        )

    if intent.operation in ("remove", "set_quantity"):
        cart_products = [
            Product(product_id=line.product_id, title=line.title) for line in cart_lines
        ]
        resolved = await _resolve_reference(intent, cart_products)
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
    intent: CartReferenceIntent, candidates: list[Product],
) -> str | None:
    """Resolves a `remove`/`set_quantity` reference against the real,
    current cart contents (`candidates`) - a single source of truth with
    no topic to get wrong, unlike "add" (see this module's own docstring
    for why "add" is never decided here after the Rev. 2 migration)."""
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
        return None  # no match, or genuinely ambiguous among the real cart lines

    return None


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
    # Focus Stack migration: `groups` is every PAUSED topic's own real
    # products, most-recently-paused first, excluding the active topic
    # entirely - `groups[0]` is the direct equivalent of the legacy
    # ledger's own `groups[-2]` ("the group immediately before this one");
    # the legacy `len(groups) < 2` minimum becomes `< 1` here for the same
    # reason (see `paused_topic_product_groups`'s own docstring).
    groups = paused_topic_product_groups(state)
    if len(groups) < 1:
        return _clarify(shown, intent, verb)

    shown_ids = {p.product_id for p in shown}
    prior_ids = [pid for pid in groups[0] if pid not in shown_ids][:4]
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
