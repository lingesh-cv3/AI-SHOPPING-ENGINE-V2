"""The chat endpoint.

Free-form shopper messages, through the same pipeline everything else uses:
reasoning proposes, the Decision Engine selects, the Risk Gate classifies, and
execution runs whatever cleared.

Two things distinguish chat from the friction path.

It reads shared memory. Before reasoning, it loads the conversation so far and any
friction the storefront already recorded against this session. A shopper whose card
was declined can open the chat and say "what now?" without explaining anything,
because the engine already knows.

Safe actions run immediately. A conversation that says "let me check" and then goes
silent for fifteen minutes waiting on an approval is not a conversation. When the
gate clears an action automatically, it executes within the turn and the result goes
into the reply. When it does not clear, the shopper is told honestly that a person is
looking - and the case is already in the queue when they are.
"""

from __future__ import annotations
from engine.decision import operation_for
from shared.models import (
    ActionType,
    CommerceError,
    FrictionType,
    ProposedAction,
)
import logging

from fastapi import Depends, APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from engine import db
from engine import session as session_store
from shared.models import CommerceError

from .auth import any_key, belongs_to
from .deps import engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/chat", tags=["chat"])


class ChatRequest(BaseModel):
    connection_id: str
    session_id: str
    message: str = Field(min_length=1, max_length=800)
    cart_id: str | None = None
    order_id: str | None = None

    #: Set when the storefront already knows what went wrong - a dead search, a
    #: declined payment. The shopper still gets a conversation; the engine just
    #: starts from a diagnosis rather than inferring one from their words.
    friction: str | None = None
    #: The search term, when the friction was a dead search. Without it the model is
    #: guessing at what they wanted.
    query: str | None = None
    #: Facts the storefront already knows, merged into whatever the model proposes.
    #:
    #: This exists because asking a model to re-derive something we already hold is
    #: adding a failure mode for nothing. When a shopper types a coupon code into a
    #: box, we have the code; the model naming it in prose and omitting it from the
    #: structured field - which is what open models reliably do - then leaves an
    #: approval nobody can action. Supplying it directly cannot fail.
    known: dict[str, str] = Field(default_factory=dict)
class ChatReply(BaseModel):
    """One assistant turn.

    action_taken is populated only when something actually ran. awaiting_person is
    the honest alternative - the shopper is told a person is looking rather than
    being given a vague acknowledgement.
    """

    reply: str
    session_id: str
    case_id: str | None = None

    used_model: bool = False
    diagnosis: str | None = None

    action_taken: str | None = None
    action_summary: str | None = None
    products: list[dict] = Field(default_factory=list)

    #: Options the shopper must pick between before the action can run. Rendered
    #: as buttons rather than asked in prose - "which size?" followed by a list
    #: they have to type back is worse than three things they can tap.
    choices: list[dict] = Field(default_factory=list)

    #: A total and the ways to pay it, when the shopper asked to check out.
    #: Rendered as buttons. Tapping one is what charges - nothing here does.
    payment: dict = Field(default_factory=dict)

    #: Options the shopper must pick between before the action can run. Rendered
    #: as buttons rather than asked in prose - "which size?" followed by a list
    #: they have to type back is worse than three things they can tap.
    choices: list[dict] = Field(default_factory=list)

    awaiting_person: bool = False
    risk_rule: str | None = None
    #: Options the shopper must pick between before the action can run. Rendered
    #: as buttons rather than asked in prose - "which size?" followed by a list
    #: they have to type back is worse than three things they can tap.
    choices: list[dict] = Field(default_factory=list)

    #: Options the shopper must pick between before the action can run. Rendered
    #: as buttons rather than asked in prose - "which size?" followed by a list
    #: they have to type back is worse than three things they can tap.
    choices: list[dict] = Field(default_factory=list)

    awaiting_person: bool = False
    risk_rule: str | None = None

    #: True when the engine actually changed the cart. The storefront refetches on
    #: this rather than guessing from the reply text - a cart that says one thing in
    #: chat and another in the sidebar is worse than no chat at all.
    cart_changed: bool = False

    #: Surfaced so the storefront can show that memory is being used rather than
    #: leaving the shopper to wonder whether it remembered.
    remembered_turns: int = 0
    remembered_friction: int = 0
    # --- pipeline trace ---
    # Included so the demo panel can render the engine's working from the same
    # response the shopper's reply came from. Two endpoints for one turn would mean
    # two model calls, which on a per-minute token budget is a real cost for a view
    # only we look at.
    proposed: list[str] = Field(default_factory=list)
    rejected: list[dict] = Field(default_factory=list)
    selected_action: str | None = None
    selection_reason: str | None = None
    escalated_because_empty: bool = False
    risk_outcome: str | None = None
    risk_rule: str | None = None
    risk_reason: str | None = None
    financial: bool = False
    model_reply: str | None = None
    evidence: list[str] = Field(default_factory=list)
    model_name: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


@router.post("", response_model=ChatReply)
async def chat(req: ChatRequest, key=Depends(any_key)) -> ChatReply:
    """Handle one shopper message."""
    belongs_to(key, req.connection_id)
    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")

    # Shared memory, both halves. Read before the new turn is recorded, so the
    # history is what was said *before* this message.
    history, friction = await session_store.context_for(
        req.session_id, req.connection_id
    )

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="shopper",
        text=req.message,
    )

    # Commerce context. Best-effort: reasoning with less is better than a turn that
    # fails because a lookup did.
    catalog_sample = None
    cart = None
    order = None
    try:
        browse = await adapter.search_products("", limit=20)
        catalog_sample = browse.products
        if req.cart_id:
            cart = await adapter.get_cart(req.cart_id)
        if req.order_id:
            order = await adapter.get_order(req.order_id)
    except CommerceError:
        pass
    friction_type = None
    if req.friction:
        try:
            friction_type = FrictionType(req.friction)
        except ValueError:
            friction_type = None

    reasoning = await engine.reasoning.reason(
        friction=friction_type,
        message=req.message,
        query=req.query,
        cart=cart,
        order=order,
        catalog_sample=catalog_sample,
        history=history,
        recorded_friction=friction,
    )
    # Merge what we already know into the proposals. Never overwrite: if the model
    # did supply a value, it saw context we did not pass here and should be trusted
    # over our guess.
    if req.known:
        for candidate in reasoning.actions:
            for field, value in req.known.items():
                candidate.parameters.setdefault(field, value)

    # The shopper's own words, carried alongside every proposal.
    #
    # Asked which size, a shopper types "7". The model then writes "I'll add it in
    # size 7" in its prose and leaves the structured field empty, so execution has
    # nothing to act on and asks again - and again, because the same thing happens
    # every turn. Watching someone answer the same question four times is what
    # prompted this.
    #
    # Passing the raw message costs nothing and cannot fail. Execution matches it
    # against the actual variant labels and ignores it when nothing matches, so a
    # shopper saying "yes please" does not accidentally choose a size.
    for candidate in reasoning.actions:
        candidate.parameters.setdefault("said", req.message[:80])
    trace = await engine.decision.decide(
        reasoning.actions, connection_id=req.connection_id, friction=friction_type
    )
    decision = engine.gate.classify(trace.selected)
    # A rules fallback has no reply of its own. Saying "let me look into that" and
    # then showing unrelated products reads as the assistant misunderstanding, when
    # what actually happened is our model provider was busy. Better to say so.
    awaiting = str(decision.outcome) != "AUTO"
    rate_limited = bool(
        reasoning.fallback_reason and "rate limit" in reasoning.fallback_reason
    )

    # One chain, in priority order. Two separate blocks meant the second silently
    # overwrote the first, so a rate-limited turn reported an approval problem
    # instead of the real cause.
    if rate_limited:
        # Say what actually happened. A held action is not the story when we could
        # not reach our model provider at all.
        reply = (
            "I'm getting a lot of questions right now - give me a few seconds and "
            "ask again."
        )
    elif trace.escalated_because_empty:
        # Everything proposed was unavailable, so the model's reply promises things
        # this platform cannot do. Replace it rather than caveat it.
        reply = (
            "I'm sorry that didn't work. I can't sort this one out myself, so "
            "I've passed it to someone at the shop who can."
        )
    elif awaiting:
        # The model wrote as if its action had run. It has not.
        reply = (
            "I can help with that, but someone at the shop needs to approve it "
            "first. I've passed it on and they'll pick it up shortly."
        )
    else:
        reply = reasoning.reply or "Let me look into that."
    case_id = None
    try:
        case_id = await db.record_case(
            connection_id=req.connection_id,
            friction=req.friction,
            query=(req.query or req.message)[:300],
            cart_id=req.cart_id,
            order_id=req.order_id,
            session_id=req.session_id,
            reasoning={
                "used_model": reasoning.used_model,
                "model_name": reasoning.model_name,
                "diagnosis": reasoning.diagnosis.cause if reasoning.diagnosis else None,
                "evidence": reasoning.diagnosis.evidence if reasoning.diagnosis else [],
                "fallback_reason": reasoning.fallback_reason,
                "model_reply": reasoning.reply,
                "shopper_reply": reply,
                "prompt_tokens": reasoning.prompt_tokens,
                "completion_tokens": reasoning.completion_tokens,
            },
            decision={
                "proposed": [
                    {
                        "action_type": str(c.action_type),
                        "parameters": c.parameters,
                        "rationale": c.rationale,
                        "confidence": c.confidence,
                    }
                    for c in reasoning.actions
                ],
                "rejected": [
                    {
                        "action_type": str(r.action_type),
                        "reason": r.reason,
                        "detail": r.detail,
                    }
                    for r in trace.rejected
                ],
                "selected_action": str(trace.selected.action.action_type),
                "selection_reason": trace.selected.selection_reason,
            },
            risk={
                "outcome": str(decision.outcome),
                "rule": decision.policy_rule,
                "reason": decision.reason,
                "financial": decision.properties.financial,
            },
            approval_timeout_minutes=engine.policies.get(
                req.connection_id
            ).approval_timeout_minutes,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record chat case; continuing")

     # Safe actions run inside the turn. A conversation that promises to check and
    # then falls silent for fifteen minutes is not a conversation.
    #
    # The outcome sentence is written here, from the execution result - never by the
    # model. The model writes its reply before anything has run, so any claim it
    # makes about what happened is a guess. Owning this sentence is what stops
    # "I've added that to your cart" appearing when nothing was added.
    action_taken = None
    action_summary = None
    products: list[dict] = []
    choices: list[dict] = []
    payment: dict = {}
    choices: list[dict] = []
    cart_changed = False

    if not awaiting and case_id:
        executed = await engine.execution.execute_case(req.connection_id, case_id)
        action_taken = executed.action_type
        action_summary = executed.summary
        found = executed.payload.get("products")
        if isinstance(found, list):
            products = found
        if executed.needs_choice:
            # Not a failure. The shopper has not said enough yet, and asking is the
            # right answer - a guessed size is a return waiting to happen.
            choices = executed.choices
            first = executed.choices[0] if executed.choices else {}
            first = executed.choices[0] if executed.choices else {}
            # Only ask if the model has not. It usually says "which size
            # would you like?" itself, and following that with our own
            # near-identical question reads as a system talking over its own
            # output.
            #
            # A trailing question mark is a crude test, and it is the right
            # kind of crude: a false negative adds a redundant sentence, a
            # false positive leaves buttons with no prompt. The first is the
            # cheaper mistake.
            if not reply.rstrip().endswith("?"):
                reply = (
                    reply
                    + "\n\nWhich option would you like for the "
                    + str(first.get("product_title", "item"))
                    + "?"
                )
        elif executed.action_type == "PREPARE_CHECKOUT" and executed.succeeded:
            payment = executed.payload
            # Appends nothing about the amount: the block below already shows
            # the total and every line, and saying it twice in different words
            # reads as a system talking over itself. Only the question is added,
            # and only when the model has not already asked one.
            if not reply.rstrip().endswith("?"):
                reply = f"{reply}\n\nHow would you like to pay?"
        elif executed.succeeded:
            if executed.action_type in {
                "ADD_TO_CART",
                "REMOVE_CART_LINE",
                "UPDATE_CART_QUANTITY",
            }:
                cart_changed = True
                count = executed.payload.get("item_count")
                total = executed.payload.get("grand_total")
                removed = executed.payload.get("removed")
                done = f"Done - {removed} is out of your cart" if removed else "Done"
                reply = (
                    f"{reply}\n\n{done}"
                    + (f". Your cart is now {count} item(s), {total}." if count is not None else ".")
                )
            elif products:
                pass  # the cards say what was found; a label announcing them is noise
            elif executed.action_type == "CHECK_AVAILABILITY":
                reply = f"{reply}\n\n{executed.summary}"
        else:
            # Say what failed rather than leaving the model's optimistic reply
            # standing alone. A shopper told "here are some options" who then sees no
            # options assumes the interface is broken.
            reply = (
                f"{reply}\n\nI couldn't turn anything up for that. Someone at the "
                "shop can help if you'd like."
            )

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="assistant",
        text=reply,
        case_id=case_id,
    )

    return ChatReply(
        reply=reply,
        session_id=req.session_id,
        case_id=case_id,
        used_model=reasoning.used_model,
        diagnosis=reasoning.diagnosis.cause if reasoning.diagnosis else None,
        action_taken=action_taken,
        action_summary=action_summary,
        products=products,
        choices=choices,
        payment=payment,
        awaiting_person=awaiting,
        risk_rule=decision.policy_rule,
        cart_changed=cart_changed,
        remembered_turns=len(history),
        remembered_friction=len(friction),
        proposed=[str(c.action_type) for c in reasoning.actions],
        rejected=[
            {"action_type": str(r.action_type), "reason": r.reason, "detail": r.detail}
            for r in trace.rejected
        ],
        selected_action=str(trace.selected.action.action_type),
        selection_reason=trace.selected.selection_reason,
        escalated_because_empty=trace.escalated_because_empty,
        risk_outcome=str(decision.outcome),
        risk_reason=decision.reason,
        financial=decision.properties.financial,
        model_reply=reasoning.reply,
        evidence=reasoning.diagnosis.evidence if reasoning.diagnosis else [],
        model_name=reasoning.model_name,
        prompt_tokens=reasoning.prompt_tokens,
        completion_tokens=reasoning.completion_tokens,
    )


@router.get("/{connection_id}/{session_id}")
async def transcript(
    connection_id: str, session_id: str, key=Depends(any_key)
) -> dict:
    """The conversation so far.

    Returns structured turns rather than formatted strings, because the widget polls
    this to pick up messages written by something other than the shopper - an
    operator approving a payment recovery, for instance. Those arrive here rather
    than as a reply to anything the shopper said.
    """
    belongs_to(key, connection_id)
    if engine.registry.adapter_for(connection_id) is None:
        raise HTTPException(404, f"unknown connection '{connection_id}'")
    return {
        "session_id": session_id,
        "turns": await session_store.turns(session_id, connection_id, limit=60),
        "friction": await session_store.recent_friction(
            session_id, connection_id, limit=5
        ),
    }


class ActRequest(BaseModel):
    """A shopper tapped something.

    Distinct from a message because there is nothing to interpret. They named a
    product and possibly an option, and both arrived as ids.
    """

    connection_id: str
    session_id: str
    product_id: str
    variant_id: str | None = None
    cart_id: str | None = None
    #: What to write in the transcript. The shopper's gesture, in words, so the
    #: conversation still reads like one.
    said: str = "Add that to my cart"


@router.post("/act", response_model=ChatReply)
async def act(req: ActRequest, key=Depends(any_key)) -> ChatReply:
    """Carry out a tap, without asking a model anything.

    The safety path is unchanged. The action still goes through the Decision Engine
    for capability and policy, and through the Risk Gate. Skipping reasoning skips
    only the part that had nothing to contribute.
    """
    belongs_to(key, req.connection_id)
    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="shopper",
        text=req.said,
    )

    parameters: dict[str, object] = {"chosen_product": req.product_id}
    if req.variant_id:
        parameters["chosen_variant"] = req.variant_id

    candidate = ProposedAction(
        action_type=ActionType.ADD_TO_CART,
        operation=operation_for(ActionType.ADD_TO_CART),
        parameters=parameters,
        rationale="the shopper tapped this",
        confidence=1.0,
    )

    trace = await engine.decision.decide(
        [candidate], connection_id=req.connection_id, friction=None
    )
    decision = engine.gate.classify(trace.selected)
    awaiting = str(decision.outcome) != "AUTO"

    case_id = None
    try:
        case_id = await db.record_case(
            connection_id=req.connection_id,
            friction=None,
            query=req.said[:300],
            cart_id=req.cart_id,
            order_id=None,
            session_id=req.session_id,
            reasoning={
                # No model was involved, and the console should say so rather than
                # implying a diagnosis nobody made.
                "used_model": False,
                "fallback_reason": "the shopper chose this directly",
                "shopper_reply": None,
            },
            decision={
                "proposed": [
                    {
                        "action_type": str(candidate.action_type),
                        "parameters": candidate.parameters,
                        "rationale": candidate.rationale,
                        "confidence": candidate.confidence,
                    }
                ],
                "rejected": [
                    {
                        "action_type": str(r.action_type),
                        "reason": r.reason,
                        "detail": r.detail,
                    }
                    for r in trace.rejected
                ],
                "selected_action": str(trace.selected.action.action_type),
                "selection_reason": trace.selected.selection_reason,
            },
            risk={
                "outcome": str(decision.outcome),
                "rule": decision.policy_rule,
                "reason": decision.reason,
                "financial": decision.properties.financial,
            },
            approval_timeout_minutes=engine.policies.get(
                req.connection_id
            ).approval_timeout_minutes,
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record a tap; continuing")

    # The product itself, for the wording below. Best-effort: a reply without the
    # product's name is worse than one with it, and not worth failing the turn over.
    product = None
    try:
        product = await adapter.get_product(req.product_id)
    except CommerceError:
        pass

    def describe() -> str:
        """The product, in a sentence a person would say.

        The merchant's description is a fragment rather than a sentence - "Built for
        milk. Chocolate, toffee, a clean finish." Joining it with a dash produced
        "Good choice - the X - built for milk": three clauses and two dashes, which
        reads like a machine. A full stop between the name and the note reads like
        someone talking.
        """
        if product is None:
            return ""
        if not product.description:
            return f"The {product.title}, good choice."
        return f"The {product.title}. {product.description.rstrip('.')}."

    reply = "Someone at the shop needs to approve that first."
    choices: list[dict] = []
    cart_changed = False
    action_summary = None

    if not awaiting and case_id:
        executed = await engine.execution.execute_case(req.connection_id, case_id)
        action_summary = executed.summary

        if executed.needs_choice:
            choices = executed.choices
            what = describe()
            reply = (f"{what} " if what else "") + "Which would you like?"
        elif executed.succeeded:
            cart_changed = True
            count = executed.payload.get("item_count")
            total = executed.payload.get("grand_total")
            chosen = next(
                (
                    v.title
                    for v in (product.variants if product else [])
                    if v.variant_id == req.variant_id
                ),
                None,
            )
            title = product.title if product else "That"
            reply = (
                f"{title}"
                + (f" ({chosen})" if chosen else "")
                + " is in your cart"
                + (
                    f" - {count} item{'' if count == 1 else 's'}, {total}."
                    if count is not None
                    else "."
                )
            )
        else:
            # executed.summary is written for an operator reading a queue. It
            # says things like "A cart and a product are both needed to add a
            # line" and, when a platform misbehaves, "no endpoint at /graphql".
            # Neither belongs in front of a shopper, and the second is close to a
            # security problem: it describes our infrastructure to a stranger.
            #
            # So the shopper gets a short, true sentence per case, and the real
            # summary goes to the queue where somebody can act on it.
            code = executed.error_code or ""
            name = product.title if product else "that"
            if code == "INVENTORY_INSUFFICIENT" or "sold out" in executed.summary:
                reply = (
                    f"{name} is sold out at the moment. Would you like me to "
                    "suggest something similar?"
                )
            elif code in {"PRODUCT_UNAVAILABLE", "VARIANT_UNAVAILABLE"}:
                reply = (
                    "I couldn't find that one - it may have just been taken "
                    "down. Have a look at what else is in the shop and I'll help "
                    "from there."
                )
            elif code == "CART_NOT_FOUND" or "cart" in executed.summary.lower():
                reply = (
                    "Something went wrong with your basket. Try refreshing the "
                    "page and I'll pick up where we left off."
                )
            else:
                reply = (
                    "I couldn't add that, sorry. Someone at the shop can help if "
                    "you'd like."
                )

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="assistant",
        text=reply,
        case_id=case_id,
    )

    return ChatReply(
        reply=reply,
        session_id=req.session_id,
        case_id=case_id,
        used_model=False,
        action_taken=str(ActionType.ADD_TO_CART),
        action_summary=action_summary,
        choices=choices,
        awaiting_person=awaiting,
        risk_rule=decision.policy_rule,
        cart_changed=cart_changed,
        selected_action=str(trace.selected.action.action_type),
        risk_outcome=str(decision.outcome),
    )


class PayRequest(BaseModel):
    """A shopper chose a card and confirmed.

    No action type corresponds to this, deliberately. The AI cannot propose it, the
    risk gate never sees it, and nothing but a shopper's own request reaches here.
    """

    connection_id: str
    session_id: str
    cart_id: str
    card_last4: str


@router.post("/pay", response_model=ChatReply)
async def pay(req: PayRequest, key=Depends(any_key)) -> ChatReply:
    """Take payment, in the conversation the shopper is already in.

    The one place in the system that charges without an approval, and the reason is
    that the person approving is the shopper - who is here, and who just tapped a
    card. Requiring anybody else's approval for somebody to buy something would be
    an odd reading of a guarantee about not spending other people's money.

    A decline is recorded as friction rather than returned as an error, so the
    existing recovery path fires: the shopper is offered help in the same
    conversation, and on a platform that supports it the money can still be
    recovered.
    """
    belongs_to(key, req.connection_id)

    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")

    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="shopper",
        text=f"Pay with the card ending {req.card_last4}",
    )

    try:
        # Derived from the cart and the card, never generated fresh.
        #
        # A shopper on a slow connection taps pay twice. Both requests arrive. With
        # the same key the platform recognises the second as a retry of the first;
        # with a random key per attempt it sees two purchases, which is exactly the
        # failure the interface makes this parameter mandatory to prevent.
        # Named idem_key, not key. `key` is already the auth record from
        # Depends(any_key), and shadowing it here meant chat() was later handed a
        # string where it expected a key - two different things sharing one name,
        # twelve lines apart.
        idem_key = f"pay-{req.cart_id}-{req.card_last4}"

        result = await adapter.checkout_with_card(
            req.cart_id, card_last4=req.card_last4, idempotency_key=idem_key
        )
    except CommerceError as exc:
        # A platform refusal is not the shopper's fault and not their business.
        logger.exception("checkout failed on %s", req.connection_id)
        reply = (
            "Something went wrong taking the payment, and it was not your card. "
            "Nothing has been charged - try again in a moment."
        )
        await session_store.add_turn(
            session_id=req.session_id,
            connection_id=req.connection_id,
            speaker="assistant",
            text=reply,
        )
        return ChatReply(
            reply=reply,
            session_id=req.session_id,
            used_model=False,
            cart_changed=False,
        )

    order = result.order
    order_id = order.order_id if order else None

    if result.succeeded:
        reply = (
            f"Paid. Your order is {order_id} and you will get a confirmation "
            "shortly. Thank you."
        )
    else:
        # Runs the decline through the same pipeline a decline anywhere else takes,
        # rather than writing a reply here and stopping. The earlier version told
        # the shopper we could help and created no case, so nobody could - and a
        # promise with no mechanism behind it is worse than saying nothing.
        #
        # This also means Kettle offers a recovery and Northfield escalates, from
        # the platform capabilities rather than from a branch in this handler.
        recovered = await chat(
            ChatRequest(
                connection_id=req.connection_id,
                session_id=req.session_id,
                message="My card was declined.",
                friction=FrictionType.PAYMENT_DECLINED,
                cart_id=req.cart_id,
                order_id=order_id,
            ),
            key=key,
        )

        # Prefixed rather than replaced. The first sentence is the fact the shopper
        # needs immediately - nothing was taken - and the rest is whatever the
        # engine decided to do about it.
        reply = (
            f"That card was declined, so nothing has been charged. "
            f"{recovered.reply}"
        )

        return ChatReply(
            reply=reply,
            session_id=req.session_id,
            case_id=recovered.case_id,
            used_model=recovered.used_model,
            action_taken=recovered.action_taken,
            action_summary=recovered.action_summary,
            awaiting_person=recovered.awaiting_person,
            risk_rule=recovered.risk_rule,
            selected_action=recovered.selected_action,
            risk_outcome=recovered.risk_outcome,
            cart_changed=True,
            payment={
                "paid": False,
                "order_id": order_id,
                "cart_retired": False,
            },
        )

    # Only the success path reaches here now. The decline path returned above,
    # after the pipeline had written its own turn - writing a second one would
    # show the shopper the same event twice.
    await session_store.add_turn(
        session_id=req.session_id,
        connection_id=req.connection_id,
        speaker="assistant",
        text=reply,
        case_id=None,
    )

    return ChatReply(
        reply=reply,
        session_id=req.session_id,
        used_model=False,
        action_taken="CHECKOUT",
        action_summary=(
            f"Shopper paid {order_id}" if result.succeeded else f"Card declined on {order_id}"
        ),
        # True either way. A declined checkout still empties nothing but the
        # storefront needs to re-read, because the order now exists.
        cart_changed=True,
        payment={
            "paid": result.succeeded,
            "order_id": order_id,
            # Tells the storefront to start a fresh cart. A paid cart is finished,
            # and leaving it on screen invites a shopper to pay it twice.
            "cart_retired": result.succeeded,
            "grand_total": str(order.grand_total) if order and order.grand_total else None,
        },
    )
