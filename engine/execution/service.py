"""Execution.

Runs an action that has already been decided, gated, and (where required)
approved. It answers no questions - by the time anything reaches here, what to do
and whether it is allowed have both been settled.

Three things this service is responsible for that nothing else can do:

Re-checking capability at execution time. The Decision Engine filtered against a
cached capability declaration, which may be up to five minutes old. A token can
expire and a scope can be revoked in that window. Checking again here is the
difference between a stale cache causing a wasted decision and a stale cache
causing a failed execution against a live merchant.

Idempotency derived from the case. The key is the case id, not a random value, so a
retried execution of the same case cannot charge a shopper twice. A random key per
attempt would defeat the entire purpose of having one.

Recording what happened. Every execution writes an outcome, including the failures.
A recovery system that only records its successes cannot tell you whether it works.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from engine import db
from engine import session as session_store
from engine.decision import CapabilityRegistry
from shared.models import (
    ActionType,
    CapabilityUnsupported,
    CaseState,
    CommerceError,
    Operation,
)

from shared.models import (
    ActionType,
    CapabilityUnsupported,
    CaseState,
    CommerceError,
    Operation,
    PaymentRecoveryMethod,
)

#: Which recovery mechanism each action asks for.
#:
#: RETRY_PAYMENT maps to retrying the same method, which is the one least likely to
#: work - a bank that just declined a card declines it again. It stays available
#: because some declines are transient (an issuer timeout), but the ranking table
#: puts it last for exactly this reason.
_RECOVERY_METHOD = {
    ActionType.RETRY_PAYMENT: PaymentRecoveryMethod.RETRY_SAME_METHOD,
    ActionType.OFFER_ALTERNATE_PAYMENT: PaymentRecoveryMethod.ALTERNATE_METHOD,
    ActionType.SPLIT_PAYMENT: PaymentRecoveryMethod.SPLIT_PAYMENT,
}

logger = logging.getLogger(__name__)

#: Actions that finish without touching the merchant's backend. Answering a
#: question and handing a case to a colleague are both real outcomes; neither is a
#: commerce operation, and treating them as failures because no API was called
#: would misreport the thing that actually happened.
NO_EXECUTION: frozenset[ActionType] = frozenset(
    {
        ActionType.ANSWER_PRODUCT_QUESTION,
        ActionType.ESCALATE_TO_HUMAN,
        ActionType.NO_ACTION,
        ActionType.NOTIFY_BACK_IN_STOCK,
    }
)


@dataclass
class Executed:
    """What happened when an action ran.

    summary is written for a person reading the queue, not for a log. An operator
    who approved something wants to know what it did, in a sentence.
    """

    succeeded: bool
    action_type: str
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    latency_ms: int | None = None
    final_state: str = str(CaseState.OUTCOME)
        #: Set when the action could not proceed because the shopper has not said enough
    #: yet - which size, which grind. Not a failure: asking is the correct outcome,
    #: and guessing a size on their behalf is how a shop ends up with a returns
    #: problem.
    needs_choice: bool = False
    #: What the shopper should be told, if anything. Deliberately separate from
    #: `summary`, which is written for an operator reading a queue. "Recovered
    #: 3422.00 INR on KB-0001" is the right sentence for the person who approved it
    #: and the wrong one for the person whose card failed.
    shopper_summary: str | None = None
    choices: list[dict[str, Any]] = field(default_factory=list)


class ExecutionService:
    """Carries out approved actions. Makes no decisions."""

    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    async def execute_case(self, connection_id: str, case_id: str) -> Executed:
        """Run the action a case selected, then record the outcome.

        Loads the case from the database rather than taking the action as an
        argument. That is deliberate: the thing executed must be the thing that was
        approved, and reading it back from storage is the only way to be sure
        nothing was substituted in between.
        """
        case = await db.get_case(connection_id, case_id)
        if case is None:
            return Executed(
                succeeded=False,
                action_type="UNKNOWN",
                summary="That case does not exist on this connection.",
                error_code="CASE_NOT_FOUND",
                final_state=str(CaseState.FAILED),
            )

        try:
            action_type = ActionType(case.selected_action or "")
        except ValueError:
            return Executed(
                succeeded=False,
                action_type=case.selected_action or "UNKNOWN",
                summary="The recorded action is not one the engine recognises.",
                error_code="VALIDATION_ERROR",
                final_state=str(CaseState.FAILED),
            )

        # Idempotency, for actions where a repeat has a cost. The key is derived
        # from the case, so a retry carries the same key as the first attempt and is
        # recognisable as a retry - a fresh random key per attempt would defeat the
        # whole mechanism.
        key = None
        if db.idempotency.guarded(action_type):
            params = self._params_for(case, action_type)
            key = db.idempotency.key_for(case.case_id, str(action_type), params)
            prior = await db.idempotency.claim(
                connection_id=connection_id,
                case_id=case.case_id,
                action_type=str(action_type),
                idempotency_key=key,
            )
            if prior is not None:
                # Already attempted. Return what happened the first time rather than
                # doing it again.
                if prior["state"] == "IN_FLIGHT":
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "An earlier attempt is unresolved - the platform may or "
                            "may not have acted. A person should check before this "
                            "is retried."
                        ),
                        error_code="IDEMPOTENCY_CONFLICT",
                        final_state=str(CaseState.FAILED),
                    )
                return Executed(
                    succeeded=bool(prior.get("succeeded")),
                    action_type=str(action_type),
                    summary=f"Already done: {prior.get('summary')}",
                    payload=prior.get("result") or {},
                    final_state=str(CaseState.OUTCOME),
                )

        result = await self._run(connection_id, case, action_type)

        if key is not None:
            await db.idempotency.complete(
                key,
                succeeded=result.succeeded,
                summary=result.summary,
                result=result.payload,
            )
        # Revenue recovered, where there is any. Only counted when money actually
        # moved - a successful search recovers a sale in spirit and nothing in fact,
        # and reporting it as revenue would inflate every number we quote.
        await db.record_outcome(
            connection_id=connection_id,
            case_id=case_id,
            resolved=result.succeeded,
            final_state=result.final_state,
            friction_type=case.friction_type,
            amount=result.payload.get("recovered_amount"),
            currency=result.payload.get("recovered_currency"),
            required_human=case.risk_outcome != "AUTO",
        )

        # Close the loop. An approval that recovers a payment is useless to the
        # shopper if nobody tells them, and "someone will pick this up" followed by
        # silence is worse than not offering.
        #
        # Only for cases that actually waited on a person. An action that ran
        # automatically already reported itself in the turn that triggered it, and
        # writing it twice would read as the assistant repeating itself.
        # What to tell the shopper, including when it did not work.
        #
        # Only successes had a shopper_summary, so an approved action that then
        # failed left the shopper waiting forever. Every other ending was already
        # covered - expiry tells them, rejection tells them, success tells them -
        # and failure was the one case left silent. That is the worst one to miss,
        # because a person actually looked at it and it still went nowhere.
        #
        # The fallback is deliberately vague about the cause. "The platform reported
        # VOUCHER_DEAD" is for the queue, not for the person who typed the code.
        told = result.shopper_summary
        if not told and not result.succeeded and not result.needs_choice:
            told = (
                "I checked with the shop and that one could not be done, sorry. "
                "If you would still like a hand, ask me and I will pass it on."
            )

        if told and case.session_id and case.risk_outcome != "AUTO":
            try:
                await session_store.add_turn(
                    session_id=case.session_id,
                    connection_id=connection_id,
                    speaker="assistant",
                    text=told,
                    case_id=case_id,
                )
            except Exception:  # noqa: BLE001
                # A failure here loses a message, not the outcome. Worth logging and
                # not worth undoing an executed action over.
                logger.exception("could not deliver the outcome to the shopper")

        return result

    @staticmethod
    def _params_for(case, action_type: ActionType) -> dict:
        """Parameters of the selected proposal, for the idempotency key."""
        for row in case.proposed or []:
            if isinstance(row, dict) and row.get("action_type") == str(action_type):
                return dict(row.get("parameters") or {})
        return {}

    # -- dispatch ----------------------------------------------------------

    async def _run(self, connection_id: str, case, action_type: ActionType) -> Executed:
        if action_type in NO_EXECUTION:
            return self._terminal(action_type, case)

        adapter = self._registry.adapter_for(connection_id)
        if adapter is None:
            return Executed(
                succeeded=False,
                action_type=str(action_type),
                summary="No adapter is connected for this merchant.",
                error_code="AUTH_FAILED",
                final_state=str(CaseState.FAILED),
            )

        # The capability re-check promised above. The decision was made against a
        # cache; this is the authoritative read.
        operation = self._operation_for(action_type)
        if operation is not None:
            caps = await self._registry.get(connection_id, force=True)
            if caps is None or not caps.supports(operation):
                self._registry.invalidate(connection_id)
                return Executed(
                    succeeded=False,
                    action_type=str(action_type),
                    summary=(
                        "This merchant's platform no longer supports that operation. "
                        "It did when the action was chosen, so the connection may "
                        "have changed."
                    ),
                    error_code="CAPABILITY_UNSUPPORTED",
                    final_state=str(CaseState.UNSUPPORTED),
                )

        started = time.perf_counter()
        try:
            result = await self._dispatch(adapter, case, action_type)
        except CapabilityUnsupported as exc:
            self._registry.invalidate(connection_id)
            return Executed(
                succeeded=False,
                action_type=str(action_type),
                summary="The platform refused: it cannot perform that operation.",
                error_code=str(exc.code),
                final_state=str(CaseState.UNSUPPORTED),
            )
        except CommerceError as exc:
            return Executed(
                succeeded=False,
                action_type=str(action_type),
                summary=f"The platform reported a problem: {exc.message}",
                error_code=str(exc.code),
                latency_ms=int((time.perf_counter() - started) * 1000),
                final_state=str(CaseState.FAILED),
            )

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    async def _dispatch(self, adapter, case, action_type: ActionType) -> Executed:
        """Map an action onto a commerce call.

        Note that the search-based actions execute by fetching the alternatives
        rather than by showing them. Execution's job is to produce the result; what
        the shopper sees is the storefront's job.
        """
               # Parameters from the *selected* action, not merged across all proposals.
        # Merging them would let a rejected proposal's search term leak into the
        # chosen one, and two proposals can legitimately carry different terms.
        params: dict[str, Any] = {}
        for row in case.proposed or []:
            if isinstance(row, dict) and row.get("action_type") == str(action_type):
                params = dict(row.get("parameters") or {})
                break

        match action_type:
            case ActionType.SUGGEST_ALTERNATIVE | ActionType.RECOMMEND_PRODUCTS:
                # The model's suggested term, or the shopper's original if it did
                # not supply one. Falling back to the original is not much use, but
                # it is honest - better than inventing a term.
                # The two actions fall back differently, and conflating them was a
                # bug worth spelling out.
                #
                # SUGGEST_ALTERNATIVE answers a search that found nothing, so the
                # original term is a sensible last resort - it at least addresses
                # what they asked for.
                #
                # RECOMMEND_PRODUCTS answers "show me something". Falling back to the
                # shopper's raw message meant a greeting was searched as a keyword:
                # "Hi" matched no product, and the assistant answered a hello with
                # "I couldn't turn anything up for that". An empty query browses the
                # catalog, which is what recommending actually means.
                if action_type is ActionType.RECOMMEND_PRODUCTS:
                    query = str(params.get("query") or "")
                else:
                    query = str(params.get("query") or case.query or "")
                found = await adapter.search_products(query, limit=6)
                titles = [p.title for p in found.products]
                # Recommending is not searching. If the model's invented term
                # finds nothing, there is still a catalogue to show - and telling
                # somebody who said hello that we could not turn anything up is
                # absurd. A genuinely failed search is SUGGEST_ALTERNATIVE, which
                # keeps its honest empty case below.
                if not titles and action_type is ActionType.RECOMMEND_PRODUCTS:
                    found = await adapter.search_products("", limit=6)
                    titles = [p.title for p in found.products]

                if not titles:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "The catalog came back empty."
                            if not query
                            else f'Searched for "{query}" and still found nothing.'
                        ),
                        payload={"query": query, "products": []},
                        final_state=str(CaseState.FAILED),
                    )
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        # An empty query is a browse, not a search for nothing.
                        # 'Searched "" and found 6' reads as a bug in the queue.
                        (
                            f"Showed {len(titles)} from the catalog: "
                            if not query
                            else f'Searched "{query}" and found {len(titles)}: '
                        )
                        + ", ".join(titles[:3])
                        + ("…" if len(titles) > 3 else "")
                    ),
                    payload={
                        "query": query,
                        "products": [
                            {
                                "product_id": p.product_id,
                                "title": p.title,
                                # The merchant's own words about the product. A card
                                # with a name and a price gives a shopper nothing to
                                # judge, so they either tap blindly or ignore it.
                                "description": p.description,
                                "price": str(p.price) if p.price else None,
                                "availability": str(p.availability),
                            }
                            for p in found.products
                        ],
                    },
                )

            case ActionType.CHECK_AVAILABILITY:
                product_id = params.get("product_id")
                if not product_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="No product was named, so there was nothing to check.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                stock = await adapter.check_inventory(product_id)
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"{product_id} is {stock.availability}"
                        + (
                            f", {stock.quantity_available} left"
                            if stock.quantity_available is not None
                            else ""
                        )
                    ),
                    payload={
                        "product_id": product_id,
                        "availability": str(stock.availability),
                        "quantity_available": stock.quantity_available,
                    },
                )
            case ActionType.ADD_TO_CART:
                product_id = params.get("chosen_product") or params.get("product_id")
                if not (product_id and case.cart_id):
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="A cart and a product are both needed to add a line.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                product = await adapter.get_product(product_id)
                buyable = [
                    v for v in product.variants if str(v.availability) != "OUT_OF_STOCK"
                ]

                # One rule: a product with options is only ever added by choosing
                # one explicitly.
                #
                # This replaced four interacting mechanisms - the model's suggested
                # variant, matching the shopper's words against labels, matching in
                # reverse for longer labels, and a separate needs-choice check. Each
                # was defensible alone. Together they let a shopper end up with two
                # bags of coffee: one added on a partial match, one added when they
                # picked properly.
                #
                # chosen_variant is set only when a shopper taps an option, so it is
                # the one signal that is unambiguously a choice they made. The
                # model's guess is ignored entirely: it cannot know a size nobody
                # stated, and once the catalog showed it real ids it started
                # supplying them confidently anyway.
                variant_id = params.get("chosen_variant")

                # A typed option, and only when the whole message is the label.
                #
                # Tapping is the intended path, but a shopper who types "8" or
                # "250g ground" in reply to being asked has plainly answered, and
                # asking again would be obtuse. The match is exact and against the
                # entire message - no containment, no partial, nothing fuzzy. That
                # restraint is deliberate: the fuzzy version is what produced two
                # bags of coffee for a shopper who wanted one.
                if not variant_id:
                    said = str(params.get("said") or "").strip().lower()
                    if said:
                        variant_id = next(
                            (
                                v.variant_id
                                for v in buyable
                                if (v.title or "").strip().lower() == said
                            ),
                            None,
                        )

                if product.variants and not variant_id:
                    if not buyable:
                        return Executed(
                            succeeded=False,
                            action_type=str(action_type),
                            summary=f"Every option of {product.title} is sold out.",
                            error_code="INVENTORY_INSUFFICIENT",
                            final_state=str(CaseState.FAILED),
                        )
                    if len(buyable) == 1:
                        # Only one thing they could mean. Asking would be pedantic.
                        variant_id = buyable[0].variant_id
                    else:
                        return Executed(
                            succeeded=False,
                            action_type=str(action_type),
                            summary=(
                                f"{product.title} comes in {len(buyable)} options - "
                                "asked which one."
                            ),
                            needs_choice=True,
                            choices=[
                                {
                                    "variant_id": v.variant_id,
                                    "label": v.title or v.variant_id,
                                    "product_id": product_id,
                                    "product_title": product.title,
                                    "left": v.quantity_available,
                                }
                                for v in buyable
                            ],
                            payload={"product_id": product_id},
                            final_state=str(CaseState.DIAGNOSED),
                        )

                # A chosen option that is not actually on this product is refused
                # rather than dropped. Silently adding the parent product with no
                # option is how an order ends up with a line nobody chose.
                if variant_id and not any(
                    v.variant_id == variant_id for v in product.variants
                ):
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=f"{variant_id} is not an option on {product.title}.",
                        error_code="VARIANT_UNAVAILABLE",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.add_to_cart(
                    case.cart_id, product_id, variant_id=variant_id
                )

                chosen = next(
                    (v.title for v in product.variants if v.variant_id == variant_id),
                    None,
                )
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Added {product.title}"
                        + (f" ({chosen})" if chosen else "")
                        + f". Cart now holds {cart.item_count} item(s), "
                        f"{cart.grand_total}."
                    ),
                    payload={
                        "cart_id": cart.cart_id,
                        "item_count": cart.item_count,
                        "grand_total": str(cart.grand_total),
                    },
                )

            case ActionType.CLEAR_CART:
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to clear.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.get_cart(case.cart_id)

                if cart.is_empty:
                    return Executed(
                        succeeded=True,
                        action_type=str(action_type),
                        summary="The cart was already empty.",
                        shopper_summary="Your cart is already empty.",
                        payload={"cart_id": case.cart_id, "item_count": 0},
                    )

                # Every line, one at a time. There is no clear-cart operation in the
                # commerce interface and adding one would mean an adapter change per
                # platform for something every platform can already do with the
                # operation it has.
                removed = [line.title for line in cart.lines]
                for line in cart.lines:
                    cart = await adapter.update_cart(
                        case.cart_id, line.line_id, quantity=0
                    )

                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Cleared {len(removed)} line(s): " + ", ".join(removed[:3])
                    ),
                    shopper_summary=(
                        f"Your cart is empty now - I took out "
                        f"{len(removed)} item{'' if len(removed) == 1 else 's'}."
                    ),
                    payload={
                        "cart_id": cart.cart_id,
                        "item_count": cart.item_count,
                        "grand_total": str(cart.grand_total),
                        "cleared": removed,
                    },
                )

            case ActionType.REMOVE_CART_LINE | ActionType.UPDATE_CART_QUANTITY:
                # The model names a product; the platform wants a line id. Reading
                # the cart to translate is the adapter boundary working as intended -
                # a shopper says "remove the racing shoe", not "remove LN00042".
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to change.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.get_cart(case.cart_id)
                wanted = str(params.get("product_id") or "").lower()
                title_hint = str(params.get("query") or "").lower()

                line = None
                for candidate in cart.lines:
                    if wanted and candidate.product_id.lower() == wanted:
                        line = candidate
                        break
                    if title_hint and title_hint in candidate.title.lower():
                        line = candidate
                        break
                # One item in the cart and no usable hint - there is only one thing
                # they can mean, and refusing on a technicality would be obtuse.
                if line is None and len(cart.lines) == 1:
                    line = cart.lines[0]

                if line is None:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "Could not tell which item they meant. Asking is better "
                            "than removing the wrong one."
                        ),
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                if action_type is ActionType.REMOVE_CART_LINE:
                    quantity = 0
                else:
                    raw_qty = params.get("quantity")
                    quantity = int(raw_qty) if str(raw_qty).isdigit() else 1

                updated = await adapter.update_cart(
                    case.cart_id, line.line_id, quantity=quantity
                )
                verb = "Removed" if quantity == 0 else f"Set to {quantity}:"
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"{verb} {line.title}. Cart now holds "
                        f"{updated.item_count} item(s), {updated.grand_total}."
                    ),
                    payload={
                        "cart_id": updated.cart_id,
                        "item_count": updated.item_count,
                        "grand_total": str(updated.grand_total),
                        "removed": line.title if quantity == 0 else None,
                    },
                )
            case ActionType.CHECK_ORDER_STATUS:
                # The number the shopper gave, or the one this session already
                # knows about. Never a number the model produced on its own - it
                # has no way to know whose order is whose, and a plausible-looking
                # id is the easiest thing in the world for it to invent.
                wanted = str(params.get("order_id") or case.order_id or "").strip()

                if not wanted:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="No order number was given, so nothing was looked up.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                try:
                    order = await adapter.get_order(wanted)
                except CommerceError:
                    # The same answer whether the order does not exist or the
                    # platform refused. Distinguishing them would let somebody
                    # walk the numbering and learn which ids are real.
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=f"No order found for {wanted}.",
                        error_code="ORDER_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                paid = str(order.payment_status) == "CAPTURED"

                # Deliberately thin. Status, payment state and total - no address,
                # no card, no line items. An order id is guessable and there is no
                # shopper identity to check it against, so what a stranger bought
                # is not something to hand out on the strength of a number.
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"{order.order_id}: {order.status}, payment "
                        f"{order.payment_status}, {order.grand_total}."
                    ),
                    shopper_summary=(
                        f"Order {order.order_id} is "
                        + (
                            f"paid in full - {order.amount_paid}. It will be on its "
                            "way shortly."
                            if paid
                            else f"still awaiting payment. The total is "
                            f"{order.grand_total} and nothing has been charged yet."
                        )
                    ),
                    payload={
                        "order_id": order.order_id,
                        "status": str(order.status),
                        "payment_status": str(order.payment_status),
                        "grand_total": str(order.grand_total)
                        if order.grand_total
                        else None,
                        "amount_paid": str(order.amount_paid)
                        if order.amount_paid
                        else None,
                        "paid": paid,
                    },
                )

            case ActionType.PREPARE_CHECKOUT:
                # Did the shopper actually ask to pay?
                #
                # Checked here rather than in the prompt, because three prompt rules
                # did not stop the model offering a payment form in reply to "hi".
                # Prompting is advice; this needs to be a fact.
                #
                # The list is short and literal on purpose. It is not trying to
                # understand intent - the model does that, and it is what got this
                # wrong. It asks a narrower question: did this person mention paying
                # at all? Somebody who did will have used one of these words.
                #
                # Erring toward refusing is the right direction. Missing a checkout
                # costs one turn. Offering one unasked is the pushy behaviour that
                # makes shoppers distrust these assistants.
                said = str(params.get("said") or case.query or "").lower()
                asked_to_pay = any(
                    word in said
                    for word in (
                        "pay",
                        "paying",
                        "payment",
                        "checkout",
                        "check out",
                        "buy",
                        "purchase",
                        "order this",
                        "place my order",
                        "card",
                        "total",
                    )
                )

                if not asked_to_pay:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "Did not offer a checkout: nothing in what the shopper "
                            "said was about paying."
                        ),
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                # Reads and reports. Charges nothing, which is why this is
                # non-financial and why the AI is allowed to propose it at all.
                if not case.cart_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="There is no cart to check out.",
                        error_code="CART_NOT_FOUND",
                        final_state=str(CaseState.FAILED),
                    )

                cart = await adapter.get_cart(case.cart_id)

                if cart.is_empty:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="The cart is empty, so there is nothing to pay for.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                # The cards are the merchant's test set rather than a real payment
                # method list, because these are demo stores. On a real platform
                # this is where the merchant's own methods would be read.
                #
                # Named honestly. A demo that hides which card declines is a demo
                # nobody can drive, and a shopper who taps "no funds" chose to.
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Showed the shopper their total, {cart.grand_total}, "
                        f"and {cart.item_count} item(s)."
                    ),
                    payload={
                        "cart_id": cart.cart_id,
                        "item_count": cart.item_count,
                        "grand_total": str(cart.grand_total),
                        "subtotal": str(cart.subtotal),
                        "lines": [
                            {
                                "title": line.title,
                                # Two lines of the same product with different
                                # variants are correct and were indistinguishable,
                                # which is what made a right answer look wrong.
                                "variant": line.variant_id,
                                "quantity": line.quantity,
                                "total": str(line.line_total),
                            }
                            for line in cart.lines
                        ],
                        "cards": [
                            {"last4": "1111", "label": "Card ending 1111"},
                            {"last4": "0002", "label": "Card ending 0002 (no funds)"},
                            {"last4": "0003", "label": "Card ending 0003 (expired)"},
                        ],
                    },
                )

            case ActionType.APPLY_PROMOTION:
                code = params.get("code") or params.get("query")
                if not (code and case.cart_id):
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            "No coupon code was recorded, so nothing could be "
                            "applied. A discount is not something to guess at."
                        ),
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )
                # Idempotency comes from the case id, so approving twice cannot
                # discount twice.
                await adapter.apply_promotion(case.cart_id, str(code))
                cart = await adapter.get_cart(case.cart_id)
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Applied {code}. Discount {cart.discount_total}, "
                        f"new total {cart.grand_total}."
                    ),
                    shopper_summary=(
                        f"Your {code} discount has been applied. Your total is now "
                        f"{cart.grand_total}."
                    ),
                    payload={
                        "code": str(code),
                        "discount": str(cart.discount_total),
                        "grand_total": str(cart.grand_total),
                    },
                )

            case (
                ActionType.RETRY_PAYMENT
                | ActionType.OFFER_ALTERNATE_PAYMENT
                | ActionType.SPLIT_PAYMENT
            ):
                # Only reachable on a platform that declared recovery supported.
                # Northfield raises CapabilityUnsupported long before this runs.
                if not case.order_id:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary="No order was recorded, so there is nothing to recover.",
                        error_code="VALIDATION_ERROR",
                        final_state=str(CaseState.FAILED),
                    )

                method = _RECOVERY_METHOD[action_type]
                key = db.idempotency.key_for(
                    case.case_id, str(action_type), {"order": case.order_id}
                )
                outcome = await adapter.recover_payment(
                    case.order_id, method=method, idempotency_key=key
                )

                if not outcome.succeeded:
                    return Executed(
                        succeeded=False,
                        action_type=str(action_type),
                        summary=(
                            f"Recovery did not work: "
                            f"{outcome.reason or 'the platform declined'}."
                        ),
                        shopper_summary=(
                            f"We tried to put order {case.order_id} through again "
                            "and it did not go through. Someone from the shop will "
                            "be in touch."
                        ),
                        payload={"method": str(method), "order_id": case.order_id},
                        error_code="PAYMENT_RECOVERY_FAILED",
                        final_state=str(CaseState.FAILED),
                    )
                amount = outcome.amount_recovered
                return Executed(
                    succeeded=True,
                    action_type=str(action_type),
                    summary=(
                        f"Recovered {amount} on {case.order_id}"
                        + (f" - {outcome.reason}." if outcome.reason else ".")
                    ),
                    shopper_summary=(
                        f"Good news - your payment for order {case.order_id} has "
                        f"gone through. Nothing more for you to do."
                    ),
                    payload={
                        "method": str(method),
                        "order_id": case.order_id,
                        # Read by record_outcome. This is the first place in the
                        # system where a real recovered amount exists.
                        "recovered_amount": str(amount.amount) if amount else None,
                        "recovered_currency": amount.currency if amount else None,
                    },
                )

        return Executed(
            succeeded=False,
            action_type=str(action_type),
            summary="The engine has no execution path for that action yet.",
            error_code="CAPABILITY_UNSUPPORTED",
            final_state=str(CaseState.UNSUPPORTED),
        )

    def _terminal(self, action_type: ActionType, case) -> Executed:
        """Actions that complete without a backend call."""
        if action_type is ActionType.ESCALATE_TO_HUMAN:
            return Executed(
                succeeded=True,
                action_type=str(action_type),
                summary=(
                    "Handed to a person. Nothing was attempted automatically, "
                    "which was the correct outcome here."
                ),
                shopper_summary=(
                    "Someone from the shop has picked this up and will be in touch."
                ),
                final_state=str(CaseState.ESCALATED),
            )
        if action_type is ActionType.NOTIFY_BACK_IN_STOCK:
            return Executed(
                succeeded=False,
                action_type=str(action_type),
                summary=(
                    "Back-in-stock alerts need an email service, which is not "
                    "built. Recorded rather than pretended."
                ),
                error_code="CAPABILITY_UNSUPPORTED",
                final_state=str(CaseState.UNSUPPORTED),
            )
        # The shopper_reply is deliberately not used here. When an action was held
        # for approval, that reply is the holding message - so echoing it back would
        # tell the approver that something needs approving, as the result of them
        # approving it. What the AI actually wanted to say is the useful thing.
        said = case.model_reply or case.shopper_reply
        return Executed(
            succeeded=True,
            action_type=str(action_type),
            summary=(
                f"Answered the shopper: \u201c{said}\u201d"
                if said
                else "Answered the shopper. No commerce operation was needed."
            ),
        )
    @staticmethod
    def _operation_for(action_type: ActionType) -> Operation | None:
        mapping = {
            ActionType.SUGGEST_ALTERNATIVE: Operation.SEARCH_PRODUCTS,
            ActionType.RECOMMEND_PRODUCTS: Operation.SEARCH_PRODUCTS,
            ActionType.CHECK_AVAILABILITY: Operation.CHECK_INVENTORY,
            ActionType.ADD_TO_CART: Operation.ADD_TO_CART,
            ActionType.UPDATE_CART_QUANTITY: Operation.UPDATE_CART,
            ActionType.REMOVE_CART_LINE: Operation.UPDATE_CART,
            ActionType.APPLY_PROMOTION: Operation.APPLY_PROMOTION,
            ActionType.RETRY_PAYMENT: Operation.RECOVER_PAYMENT,
            ActionType.OFFER_ALTERNATE_PAYMENT: Operation.RECOVER_PAYMENT,
            ActionType.SPLIT_PAYMENT: Operation.RECOVER_PAYMENT,
            ActionType.ISSUE_REFUND: Operation.RECOVER_PAYMENT,
        }
        return mapping.get(action_type)
