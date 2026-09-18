"""Shopper V2, Stage 1 - discovery-only, read-only.

A new, additive route. Reuses the exact auth/ownership dependencies
`engine/api/chat.py` already uses (`any_key`, `belongs_to`, `visitor`) -
no new auth code. Does not import or modify `engine.api.chat`, and nothing
in `engine.api.chat` imports this module - V1 is completely unaffected by
this file's existence.

This route never calls `engine.decision`, `engine.risk`, or
`engine.execution`'s mutation paths, never proposes or executes an
`ActionType`, and never touches cart/checkout/payment - see
`engine/shopper_v2/orchestrator.py`'s own docstring for the enforced call
order.
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


class ChatReplyV2(BaseModel):
    reply: str
    shopping_state: ShoppingStateV2
    evidence: TurnEvidence
    used_model: bool


@router.post("", response_model=ChatReplyV2)
async def chat_v2(
    req: ChatRequestV2,
    key=Depends(any_key),
    who: Visitor = Depends(visitor),
) -> ChatReplyV2:
    """One Stage 1 shopper turn. Read-only: no cart, no checkout, no
    payment, no adapter mutation - see module docstring."""
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

    adapter = engine.registry.adapter_for(req.connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{req.connection_id}'")

    result = await run_turn(
        reasoning=engine.reasoning,
        adapter=adapter,
        session_id=req.session_id,
        connection_id=req.connection_id,
        message=req.message,
    )

    return ChatReplyV2(
        reply=result.reply,
        shopping_state=result.state,
        evidence=result.evidence,
        used_model=result.used_model,
    )
