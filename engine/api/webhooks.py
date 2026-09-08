"""Webhook intake - a merchant's own platform reporting friction.

The storefront reports friction because it watches a shopper directly: a dead
search, a declined payment, a cart nobody returned to. A merchant's own
backend can report the same events another way - their platform emits a
webhook the moment something happens, whether or not our storefront is even
open. `shared/models/events.py` already names this as the second of three ways
a `Signal` can enter the engine (`WIDGET`, `MERCHANT_WEBHOOK`, `ENGINE`), and
states the design plainly: "nothing above the intake knows or cares which
source a signal came from." This route is what makes that true for the second
source - it hands every verified signal to the exact same
`engine.api.chat._process_turn` pipeline the storefront's own messages run
through, so no piece of reasoning, decision, risk or execution is duplicated
for this second entry point.

Authentication is different in kind here, not absent. A shopper's request is
authenticated with a publishable key plus a visitor cookie; a webhook has
neither, because the caller is the merchant's own backend, not a browser. It
is authenticated by `SupportsWebhooks.verify_webhook` instead - each adapter
validates its own platform's signature scheme, and an unverified webhook is
discarded before it is ever parsed, per that interface's own docstring. This
route does not, and must not, accept a publishable or secret key as a
substitute: a key proves "this browser/server holds a credential we issued",
not "this event genuinely originated at the merchant's platform", and only
the platform's own signature can prove that.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request

from shared.interfaces import SupportsWebhooks
from shared.models import Signal

from .chat import ChatRequest, _process_turn
from .deps import engine

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


@router.post("/{connection_id}")
async def receive_webhook(connection_id: str, request: Request) -> dict:
    """One merchant platform's event, verified, parsed, and run through the pipeline.

    Never trusts a status code to a caller that has not been verified: an
    unknown connection and a platform that never declared webhook support
    both read as 404 - the same "does this exist for you" answer any other
    locked route gives a stranger, so a prober cannot tell "wrong connection"
    from "not a real merchant" from the response alone. An unverified
    signature is 401, and nothing after that line ever runs - discarded
    before parsing, never partially trusted.
    """
    adapter = engine.registry.adapter_for(connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{connection_id}'")
    if not isinstance(adapter, SupportsWebhooks):
        raise HTTPException(404, "this platform has not declared webhook support")

    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}

    verified = await adapter.verify_webhook(headers, body)
    if not verified:
        raise HTTPException(401, "could not verify this webhook")

    signals = await adapter.parse_webhook(headers, body)

    processed = []
    for signal in signals:
        try:
            reply = await _handle_signal(connection_id, signal)
            processed.append({"signal_id": signal.signal_id, "case_id": reply.case_id})
        except Exception:  # noqa: BLE001
            # One bad signal in a batch should not sink the rest, and a
            # platform retrying a whole delivery because one signal in it
            # failed to process would just repeat the same partial failure.
            logger.exception(
                "could not process webhook signal %s for %s",
                signal.signal_id,
                connection_id,
            )
            processed.append({"signal_id": signal.signal_id, "case_id": None})

    return {"received": len(signals), "processed": processed}


async def _handle_signal(connection_id: str, signal: Signal):
    """Translate one normalized Signal into a turn through the shared pipeline.

    Reuses the shopper's own session if the platform tagged the event with
    one - then a shopper mid-chat sees the outcome arrive through the poll,
    the same way an operator's approval decision already does. Without one, a
    fresh session is minted so the case still has somewhere to live; nobody
    is watching for a reply, but the case, its risk decision and its outcome
    are recorded exactly the same way either way.
    """
    session_id = signal.session_id or f"webhook_{signal.signal_id}"

    req = ChatRequest(
        connection_id=connection_id,
        session_id=session_id,
        message=_describe(signal),
        cart_id=signal.cart_id,
        order_id=signal.order_id,
        friction=str(signal.friction_type),
        query=signal.search_query,
        # Never the shopper's own words - nobody typed this, a platform
        # reported it. synthetic=True skips writing it as a shopper turn,
        # the same guarantee /api/chat/pay's decline recursion relies on
        # for the same reason (see chat.py).
        synthetic=True,
    )
    return await _process_turn(req)


def _describe(signal: Signal) -> str:
    """A plain-language stand-in for what the platform reported.

    Exists only so the reasoning layer has a message to reason over, the same
    shape every other friction turn already has - never shown to a shopper as
    their own words, since `synthetic=True` above skips recording it as one.
    """
    parts = [f"[{signal.source}] {signal.friction_type}"]
    if signal.search_query:
        parts.append(f'query="{signal.search_query}"')
    if signal.decline_reason:
        parts.append(f"reason={signal.decline_reason}")
    if signal.pre_diagnosed_cause:
        parts.append(signal.pre_diagnosed_cause)
    return " ".join(parts)
