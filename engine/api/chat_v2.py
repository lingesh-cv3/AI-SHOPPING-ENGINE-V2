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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from engine import db
from engine.shopper_v2.evidence import TurnEvidence
from engine.shopper_v2.orchestrator import run_turn
from engine.shopper_v2.state import ShoppingStateV2

from .auth import Visitor, any_key, belongs_to, require_owner, visitor
from .deps import engine

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

    result = await run_turn(
        reasoning=engine.reasoning,
        adapter=adapter,
        session_id=req.session_id,
        connection_id=req.connection_id,
        message=req.message,
        cart_id=req.cart_id,
        decision=engine.decision,
        gate=engine.gate,
        execution=engine.execution,
        owner_keys=await who.keys_for(req.connection_id),
        shopper_id=await who.shopper_id_for(req.connection_id),
    )

    return ChatReplyV2(
        reply=result.reply,
        shopping_state=result.state,
        evidence=result.evidence,
        used_model=result.used_model,
        trace=result.trace,
        products=[
            ProductSummaryV2(
                product_id=p.product_id,
                title=p.title,
                description=p.description,
                price=str(p.price) if p.price is not None else None,
                image_url=p.image_url,
            )
            for p in result.products
        ],
    )
