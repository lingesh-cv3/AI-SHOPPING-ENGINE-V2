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
from shared.models import CommerceError, FrictionType
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from engine import db
from engine import session as session_store
from shared.models import CommerceError

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

    awaiting_person: bool = False
    risk_rule: str | None = None
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
async def chat(req: ChatRequest) -> ChatReply:
    """Handle one shopper message."""
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
    cart_changed = False

    if not awaiting and case_id:
        executed = await engine.execution.execute_case(req.connection_id, case_id)
        action_taken = executed.action_type
        action_summary = executed.summary
        found = executed.payload.get("products")
        if isinstance(found, list):
            products = found

        if executed.succeeded:
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
                reply = f"{reply}\n\nHere's what I found:"
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
async def transcript(connection_id: str, session_id: str) -> dict:
    """The conversation so far.

    Returns structured turns rather than formatted strings, because the widget polls
    this to pick up messages written by something other than the shopper - an
    operator approving a payment recovery, for instance. Those arrive here rather
    than as a reply to anything the shopper said.
    """
    if engine.registry.adapter_for(connection_id) is None:
        raise HTTPException(404, f"unknown connection '{connection_id}'")
    return {
        "session_id": session_id,
        "turns": await session_store.turns(session_id, connection_id, limit=60),
        "friction": await session_store.recent_friction(
            session_id, connection_id, limit=5
        ),
    }