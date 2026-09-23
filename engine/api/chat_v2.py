"""Shopper V2 - discovery (Stage 1) plus, as of Phase 4, cart proposals.

A new, additive route. Reuses the exact auth/ownership dependencies
`engine/api/chat.py` already uses (`any_key`, `belongs_to`, `visitor`,
and - now that a `cart_id` can be present - the same `db.owners.CART`
ownership check `engine/api/chat.py::_mine` already applies) - no new auth
code. Does not import or modify `engine.api.chat`, and nothing in
`engine.api.chat` imports this module - V1 is completely unaffected by
this file's existence.

This route reuses `engine.decision`/`engine.risk`/`engine.execution` as
opaque objects, passed straight through to `orchestrator.py` exactly as
`engine/api/chat.py` already constructs and uses them (same shared `engine`
singleton, same `db.record_case`/`execute_case` functions) - never
reimplemented. Every cart-affecting path still resolves to a
`PROPOSE_CART_CHANGE` proposal, never a direct mutation - see
`engine/shopper_v2/cart_proposal.py`'s own docstring for the full safety
argument. Shoppers approve or reject a V2-created proposal through the
EXISTING, unmodified `POST /api/chat/proposals/{id}/accept` /
`/reject` routes - no new V2 approval endpoint was added.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from engine import db
from engine import session as session_store
from engine.execution import events as shopper_events
from engine.execution import return_risk
from engine.shopper_v2.evidence import TurnEvidence, build_why_v2
from engine.shopper_v2.orchestrator import run_turn
from engine.shopper_v2.state import ShoppingStateV2

from .auth import Visitor, any_key, belongs_to, require_owner, visitor
from .deps import engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v2/chat", tags=["shopper-v2"])


class ChatRequestV2(BaseModel):
    connection_id: str
    session_id: str
    message: str = Field(min_length=1, max_length=800)
    #: Optional. Required only for a turn that proposes a cart change
    #: (Capability.CART_ACTION) - a discovery/compare turn needs none.
    #: When present, ownership is checked exactly like V1's own
    #: `_mine(..., cart_id=...)` - a stranger cannot name another
    #: shopper's cart_id here any more than on `/api/chat`.
    cart_id: str | None = None
    #: Optional, explicit friction-type signal - the V2 equivalent of V1's
    #: own `friction` parameter (`engine/api/chat.py`), previously entirely
    #: absent from this route (a real, disclosed V1/V2 parity gap -
    #: `storefront/src/App.tsx`'s own comments already named it). When set,
    #: this bypasses `capability.py::route`'s model-hint classification
    #: entirely and routes deterministically - the frontend already knows
    #: precisely which friction happened (a dead search, a rejected promo
    #: code, a declined payment); asking the model to re-infer it from a
    #: synthesized sentence is strictly worse than just being told.
    friction: str | None = None


class ProductSummaryV2(BaseModel):
    """Real, adapter-fetched fields only - for the frontend to render a
    product card. Never a field the model could have invented; built
    directly from the same `Product` objects `evidence.py` already
    verified against."""

    product_id: str
    title: str
    description: str | None = None
    price: str | None = None
    image_url: str | None = None
    #: Real, adapter-declared stock status - lets the frontend render the
    #: same "in stock"/"out of stock" fact V1's own comparison table
    #: already shows, from data already fetched here, never invented.
    availability: str | None = None
    #: Real, evidence-gated return-risk findings for this product, if any -
    #: see `engine/execution/return_risk.py`'s own module docstring for the
    #: two kinds this can ever be (`known_fact` from a real low rating with
    #: enough reviews to trust it, or `inferred_concern` from a stated
    #: requirement the product's own text doesn't confirm). Never present
    #: without real supporting evidence - None/empty means no finding, not
    #: "safe," since this system has no return-history data to certify
    #: safety from either way. V1-parity field: V1 already surfaces this
    #: (engine/api/chat.py); it was a real gap that V2, now the default
    #: assistant, never had before.
    return_risk: list[dict] | None = None


class ChatReplyV2(BaseModel):
    reply: str
    shopping_state: ShoppingStateV2
    evidence: TurnEvidence
    used_model: bool
    #: Structured per-turn trace (see engine/shopper_v2/tracing.py) -
    #: surfaced on the response itself, not only in logs, so a failure's
    #: stage is inspectable without grepping (Phase 1 observability
    #: requirement). Plain dict, not a typed model: its shape is allowed to
    #: grow without a response-model migration.
    trace: dict
    #: The real products shown/verified this turn, if any - see
    #: `orchestrator.TurnResultV2.products`'s own docstring.
    products: list[ProductSummaryV2] = Field(default_factory=list)
    #: V1-parity field (Conversational-flow review fix, Issue #7): built by
    #: `evidence.build_why_v2` from this SAME turn's real evidence/shopping
    #: state - `None` when there is nothing grounded to show. See that
    #: function's own docstring for why V1's `engine/api/why.py::explain`
    #: (built around rejected ACTIONS) is not reused as-is here.
    why: dict | None = None


@router.post("", response_model=ChatReplyV2)
async def chat_v2(
    req: ChatRequestV2,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> ChatReplyV2:
    """One shopper turn. Discovery/compare are read-only, exactly as
    before; a cart-proposal turn only ever creates a proposal, never a
    direct mutation - see module docstring."""
    belongs_to(key, req.connection_id)
    await require_owner(
        who,
        req.connection_id,
        db.owners.SESSION,
        req.session_id,
        code="SESSION_NOT_FOUND",
        message="no such conversation",
        read_only=False,
    )
    if req.cart_id:
        await require_owner(
            who,
            req.connection_id,
            db.owners.CART,
            req.cart_id,
            code="CART_NOT_FOUND",
            message="no such cart",
        )

    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")

    shopper_id = await who.shopper_id_for(req.connection_id)

    # Durable, signed-in-shopper-only facts from a previous visit - the same
    # read V1's `engine/api/chat.py` already does (`db.memory_facts`,
    # unmodified) before reasoning runs, so a V2 turn can be genuinely
    # personalized too, not just discovery-grounded. `orchestrator.run_turn`
    # already accepts `memory_facts`; this route previously never fetched
    # it, which meant a signed-in shopper's real memory facts existed in the
    # database but could never reach a V2 turn - a real V1-vs-V2 parity gap,
    # not a deliberate omission. Best-effort, like V1's own read: a turn
    # should not fail because this hiccuped.
    memory_facts: list[dict] = []
    try:
        if shopper_id:
            memory_facts = await db.memory_facts.for_shopper(req.connection_id, shopper_id)
    except Exception:  # noqa: BLE001
        pass

    result = await run_turn(
        reasoning=engine.reasoning,
        adapter=adapter,
        session_id=req.session_id,
        connection_id=req.connection_id,
        message=req.message,
        friction=req.friction,
        memory_facts=memory_facts or None,
        cart_id=req.cart_id,
        decision=engine.decision,
        gate=engine.gate,
        execution=engine.execution,
        owner_keys=await who.keys_for(req.connection_id),
        shopper_id=shopper_id,
    )

    evidence_fields_by_id = {
        e.product_id: e.evidence_fields for e in result.evidence.products
    }
    product_dicts = [
        {
            "product_id": p.product_id,
            "title": p.title,
            "description": p.description,
            "price": str(p.price) if p.price is not None else None,
            "categories": [],
            "fit": evidence_fields_by_id.get(p.product_id) or None,
        }
        for p in result.products
    ]

    # Return-risk parity with V1 (engine/api/chat.py) - identical
    # deterministic function, identical evidence-gating, only reached here
    # too now that V2 is the default assistant (see PROGRESS.md: this was a
    # real gap, not a stylistic omission - a finding that only ever fired
    # through a path shoppers no longer take is an unreachable claim).
    # `catalog_sample` is the SAME already-fetched set the shopper was just
    # shown, never a second fetch - `_better_rated_alternative` only ever
    # names a real product from it.
    risk_catalog_sample = [
        {
            "product_id": p.product_id,
            "title": p.title,
            "description": p.description,
            "categories": list(p.categories or []),
            "rating": p.rating,
            "rating_count": p.rating_count,
            "availability": str(p.availability) if p.availability is not None else None,
        }
        for p in result.products
    ]
    return_risk_by_id: dict[str, list[dict]] = {}
    for p, risk_dict in zip(result.products, risk_catalog_sample):
        findings = return_risk.assess_product(
            risk_dict, result.state.as_prompt_dict(), catalog_sample=risk_catalog_sample
        )
        if not findings:
            continue
        return_risk_by_id[p.product_id] = [
            {
                "kind": f.kind,
                "evidence_type": f.evidence_type,
                "message": f.message,
                "safer_alternative_id": f.safer_alternative_id,
            }
            for f in findings
        ]
        try:
            await shopper_events.record(
                connection_id=req.connection_id,
                session_id=req.session_id,
                event_type=shopper_events.EventType.RETURN_RISK_FLAGGED,
                shopper_id=shopper_id,
                product_id=p.product_id,
                detail={"kinds": [f.kind for f in findings]},
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record return_risk_flagged; continuing")

    capability = result.trace.get("capability")
    is_comparison = capability == "compare" and len(product_dicts) == 2
    comparison_dicts = (
        [
            {**pd, "availability": str(p.availability)}
            for pd, p in zip(product_dicts, result.products)
        ]
        if is_comparison
        else None
    )
    payment_dict = result.trace.get("checkout_payload") if capability == "checkout" else None

    # Persisted into the SAME session-transcript store V1's own
    # `engine/api/chat.py` already writes to (`engine/session/store.py`,
    # unmodified) - not a second storage mechanism. This is what keeps the
    # existing ChatWidget UI's restore-on-reload and async poll-for-updates
    # (e.g. an operator resolving a payment recovery case) working
    # identically for a V2-powered turn, with no new persistence path.
    try:
        await session_store.add_turn(
            session_id=req.session_id,
            connection_id=req.connection_id,
            speaker="shopper",
            text=req.message,
        )
        await session_store.add_turn(
            session_id=req.session_id,
            connection_id=req.connection_id,
            speaker="assistant",
            text=result.reply,
            products=product_dicts if not is_comparison else None,
            comparison=comparison_dicts,
            payment=payment_dict,
        )
    except Exception:  # noqa: BLE001 - best-effort, never fail the turn over it
        pass

    product_summaries = [
        ProductSummaryV2(
            product_id=p.product_id,
            title=p.title,
            description=p.description,
            price=str(p.price) if p.price is not None else None,
            image_url=p.image_url,
            availability=str(p.availability) if p.availability is not None else None,
            return_risk=return_risk_by_id.get(p.product_id),
        )
        for p in result.products
    ]

    return ChatReplyV2(
        reply=result.reply,
        shopping_state=result.state,
        evidence=result.evidence,
        used_model=result.used_model,
        trace=result.trace,
        products=product_summaries,
        why=build_why_v2(result.evidence, result.state.as_prompt_dict(), product_summaries),
    )
