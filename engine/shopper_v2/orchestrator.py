"""The Shopper V2 turn orchestrator. The one place that assembles the whole
loop, and the one place whose CALL ORDER is the actual enforcement of the
core Stage 1 invariant:

    message -> understand -> StateDelta -> ShoppingStateV2 -> context
            -> capability routing -> retrieval -> evidence -> respond

`respond()` is called LAST, and its signature (see `respond.py`) accepts
only the post-retrieval `TurnEvidence`, the real fetched `Product`s, and the
current `ShoppingStateV2` - there is no path through this function that can
reach a response before retrieval and evidence exist.

Phase 1 adds capability routing (`capability.py`) and structured tracing
(`tracing.py`) on top of Stage 1's original shape, and Phase 1/2 replaces
the inline retrieval+evidence calls with `pipeline.run_discovery_turn`,
which wires the Stage 2A typed contracts into this live path and closes
Stage 2A Findings 4 and 5 (PROGRESS.md, Shopper V2 §E/§F). Phase 3 (first
slice) adds `Capability.COMPARE` (`compare.py`), resolved from
`ShoppingStateV2.active_products` - populated here, at the end of a
DISCOVER turn, from whatever was actually verified and shown.

**Phase 4 (first slice) adds `Capability.CART_ACTION` (`cart_proposal.py`)
- the first and only place in `engine/shopper_v2/` that reaches the
Decision Engine, Risk Gate, and ExecutionService, and it does so by calling
their existing, unmodified public methods (`decision.decide`,
`gate.classify`, `execution.execute_case`, plus `engine.db.record_case` -
the exact same functions `engine/api/chat.py` itself calls), never by
reimplementing any of them.** Every path through `cart_proposal.py` builds
one thing only: an `ActionType.PROPOSE_CART_CHANGE` candidate - never
`ADD_TO_CART`/`REMOVE_CART_LINE`/`UPDATE_CART_QUANTITY`/`CLEAR_CART`
directly - so a cart can only ever actually change after a shopper's
SEPARATE, explicit call to the existing, unmodified
`POST /api/chat/proposals/{id}/accept` route. See `cart_proposal.py`'s own
docstring for the full call path and the safety argument.

**Phase 5 adds `Capability.CHECKOUT` (`checkout.py`, `PREPARE_CHECKOUT` -
non-financial, completes in the same turn, no path to `/api/chat/pay`) and
`Capability.PAYMENT_RECOVERY` (`payment_recovery.py`) - the latter proposes
all three recovery `ActionType`s and lets the real, unmodified
`DecisionEngine` decide what survives capability filtering; every recovery
action type is `financial=True` (`shared/models/action.py`, unmodified),
so the Risk Gate's `FINANCIAL_ALWAYS_HUMAN` rule always classifies it
`HUMAN` - this module never calls `execution.execute_case` for an actual
recovery action, only records the case (which is itself enough to place it
in the real ops queue). Only the non-financial `ESCALATE_TO_HUMAN` (when a
platform never declared recovery capability at all) is ever executed. See
`payment_recovery.py`'s own docstring for the full argument.

Every other capability still gets an honest "not yet available" reply
rather than silently being treated as discovery.

This module imports `engine.decision`/`engine.risk`/`engine.execution`
ONLY as opaque objects passed in by the caller (`chat_v2.py`, which already
constructs them via the shared `engine` singleton V1 also uses) and passed
straight through to `cart_proposal.py` - it never imports `ActionType` or
`ProposedAction` itself, and never calls any adapter mutation method
directly. See `tests/shopper_v2/test_no_mutation_imports.py`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from shared.models import Product

from engine.db import shopping_state as shopping_state_store
from engine.execution import events as shopper_events
from engine.reasoning.service import ReasoningService

from .capability import Capability, route
from .cart_proposal import run_cart_change_proposal_turn
from .checkout import run_checkout_turn
from .compare import run_compare_turn
from .compatibility import run_compatibility_turn, run_complementary_turn
from .context import build_turn_context
from .cart_status import run_cart_status_turn
from .order_status import run_order_status_turn
from .payment_recovery import run_payment_recovery_turn
from .evidence import TurnEvidence, build_evidence
from .intent_resolution import resolve_cart_target
from .missions import run_mission_turn
from .pipeline import run_discovery_turn
from .respond import respond as respond_
from .state import ShoppingStateV2, apply_delta, load
from .tracing import TurnTrace
from .tradeoff import run_tradeoff_turn
from .understanding import understand

logger = logging.getLogger(__name__)


async def _record_event(
    *,
    connection_id: str,
    session_id: str,
    shopper_id: str | None,
    event_type: str,
    product_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Best-effort, never allowed to fail a turn - the exact discipline
    `engine/api/chat.py` already applies at every one of its own
    `shopper_events.record` call sites. Writes through the EXISTING
    `engine.execution.events` module/table - no second measurement system."""
    try:
        await shopper_events.record(
            connection_id=connection_id,
            session_id=session_id,
            shopper_id=shopper_id,
            event_type=event_type,
            product_id=product_id,
            detail=detail,
        )
    except Exception:  # noqa: BLE001
        logger.exception("shopper_v2: could not record event %r; continuing", event_type)


async def _record_impressions(
    *, connection_id: str, session_id: str, shopper_id: str | None, product_ids: list[str],
) -> None:
    """Best-effort, same discipline as `_record_event` - one row per shown
    product, via the EXISTING `record_many`."""
    if not product_ids:
        return
    try:
        await shopper_events.record_many(
            connection_id=connection_id,
            session_id=session_id,
            shopper_id=shopper_id,
            event_type=shopper_events.EventType.PRODUCT_IMPRESSION,
            product_ids=product_ids,
        )
    except Exception:  # noqa: BLE001
        logger.exception("shopper_v2: could not record product impressions; continuing")


@dataclass
class TurnResultV2:
    reply: str
    state: ShoppingStateV2
    evidence: TurnEvidence
    used_model: bool
    trace: dict
    #: The real, adapter-fetched products shown/verified this turn, if
    #: any - for the frontend to render as cards. Empty for capabilities
    #: that show no product list of their own (cart proposal/checkout/
    #: payment recovery already name the product in `reply` itself).
    products: list[Product] = field(default_factory=list)


_UNIMPLEMENTED_CAPABILITY_REPLIES: dict[Capability, str] = {
    Capability.TRADE_OFF: (
        "I can't re-rank based on a stated trade-off through this "
        "conversation path yet - it's a planned capability, not something "
        "I can do here today."
    ),
    Capability.UNKNOWN: (
        "I'm not sure how to help with that through this conversation "
        "path yet - I can help you find products right now."
    ),
}


async def run_turn(
    *,
    reasoning: ReasoningService,
    adapter,
    session_id: str,
    connection_id: str,
    message: str,
    memory_facts: list[dict] | None = None,
    catalog_sample: list | None = None,
    cart_id: str | None = None,
    decision=None,
    gate=None,
    execution=None,
    owner_keys: list[str] | None = None,
    shopper_id: str | None = None,
) -> TurnResultV2:
    """Run one V2 turn, read-only, start to finish."""
    trace = TurnTrace(session_id=session_id, connection_id=connection_id)

    # 1. Load current typed state.
    current_state = await load(session_id, connection_id)

    # 2. Bounded context (history/friction) - existing session store, unmodified.
    turn_context = await build_turn_context(session_id, connection_id)

    # 3. Understanding -> StateDelta (+ a read-only capability hint - see
    #    understanding.py's own docstring, invariant 2). The model's `reply`
    #    and every proposed action's parameters are discarded inside
    #    `understand()` itself - nothing here ever sees them.
    result = await understand(
        reasoning=reasoning,
        message=message,
        history=turn_context.history,
        shopping_state=current_state.as_prompt_dict(),
        memory_facts=memory_facts,
    )
    delta = result.delta
    trace.understanding_used_model = result.used_model
    trace.understanding_model_name = result.model_name

    # 4. Apply the delta -> new typed ShoppingStateV2 (existing persistence
    #    + existing per-session lock, reused unmodified; version incremented
    #    in-memory only - see state.py's own docstring).
    new_state = await apply_delta(current_state, delta)
    trace.state_version = new_state.version

    # 4b. Multi-item shopping missions - a SECOND, narrow, bounded LLM call
    # (`ReasoningService.interpret_mission_items`, see missions.py's own
    # docstring), attempted only when there is already a tracked mission
    # (continuing one) or this message plausibly introduces more than one
    # item (a cheap, cost-only gate - never a correctness decision; a
    # missed gate here just means an ordinary single-item discovery turn
    # runs instead, the existing safe default, never a wrong answer). The
    # actual decision of what changed is always the bounded interpreter's,
    # deterministically validated against the real tracked item list -
    # never this heuristic's.
    if reasoning is not None and (
        new_state.mission_items
        or any(sep in message.lower() for sep in (",", " and ", " & "))
    ):
        mission_reply, mission_products, mission_state, mission_handled, mission_proposal_ids = await run_mission_turn(
            reasoning=reasoning,
            adapter=adapter,
            message=message,
            history=turn_context.history,
            state=new_state,
            decision=decision,
            gate=gate,
            execution=execution,
            connection_id=connection_id,
            session_id=session_id,
            cart_id=cart_id,
        )
        if mission_handled:
            new_state = mission_state
            trace.capability = "mission"
            evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
            trace.response_used_model = False
            await _record_event(
                connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
                event_type=shopper_events.EventType.MISSION_ITEM_TRACKED,
                detail={"item_count": len(new_state.mission_items)},
            )
            if mission_proposal_ids:
                await _record_event(
                    connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
                    event_type=shopper_events.EventType.BASKET_PROPOSED,
                    detail={"proposal_ids": mission_proposal_ids},
                )
            trace.finish()
            return TurnResultV2(
                reply=mission_reply,
                state=new_state,
                evidence=evidence,
                used_model=False,
                trace={**trace.as_dict(), "mission_proposal_ids": mission_proposal_ids},
                products=mission_products,
            )

    # 4c. Trade-off reasoning and re-ranking - a purely deterministic
    # trigger, not a new bounded LLM call: `delta.priorities_add` already
    # arrived on THIS turn's `StateDelta` via the existing `understand()`
    # call (see tradeoff.py's own docstring). When this turn stated a new
    # priority AND there is something real currently in view to re-rank
    # (`active_products`, set by a prior discovery turn), re-run the
    # EXISTING `mission_ranking.rank_and_filter` against the now-updated
    # priorities and report the real, grounded result - never silently
    # absorbing "actually X matters more" into state without ever telling
    # the shopper what it changed. A priority statement with nothing yet
    # shown falls through to ordinary discovery unchanged (there is
    # nothing to re-rank against yet).
    if delta.priorities_add and new_state.active_products:
        tradeoff_reply, tradeoff_evidence, tradeoff_products, reordered_ids = await run_tradeoff_turn(
            adapter=adapter, message=message, state=new_state,
        )
        trace.capability = "trade_off"
        trace.response_used_model = False
        if reordered_ids:
            await shopping_state_store.merge(
                session_id, connection_id, active_products=reordered_ids
            )
            new_state.active_products = reordered_ids
        await _record_event(
            connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
            event_type=shopper_events.EventType.TRADE_OFF_APPLIED,
            detail={"priorities": list(new_state.priorities)},
        )
        trace.finish()
        return TurnResultV2(
            reply=tradeoff_reply,
            state=new_state,
            evidence=tradeoff_evidence,
            used_model=False,
            trace=trace.as_dict(),
            products=tradeoff_products,
        )

    # 5. Capability routing - a read-only classification, never an
    #    execution decision. Only DISCOVER is implemented; everything else
    #    gets an honest, specific "not yet available" reply.
    capability = route(result.hinted_action_type)
    hinted_product_id = result.hinted_product_id
    hinted_quantity: int | None = None
    cart_operation = "add"

    # 5b. General cart-intent interpretation (Phase 4 continuation, widened
    # to cover remove/set_quantity/clear) - a SECOND, narrow, bounded LLM
    # call (`ReasoningService.interpret_cart_reference`) that handles the
    # general class of "add that"/"the first one"/"remove the shoes"/"make
    # that quantity two"/"clear my cart", validated deterministically
    # against real shown/cart data - see intent_resolution.py's own
    # docstring. Only attempted when there is decision/gate/execution to
    # act on and something currently shown OR in the real cart to refer to;
    # a plain discovery turn with nothing shown and an empty cart always
    # short-circuits to `is_cart_intent=False` inside `resolve_cart_target`
    # itself, so this can never turn a genuine discovery statement into a
    # cart action - requirement 9's guarantee.
    if decision is not None and gate is not None and execution is not None:
        cart_target = await resolve_cart_target(
            reasoning=reasoning,
            adapter=adapter,
            message=message,
            history=turn_context.history,
            state=new_state,
            cart_id=cart_id,
        )
        trace.cart_intent_checked = True
        trace.cart_intent_detected = cart_target.is_cart_intent
        if cart_target.is_cart_intent:
            if cart_target.operation == "clear" or cart_target.product_id:
                capability = Capability.CART_ACTION
                cart_operation = cart_target.operation
                hinted_product_id = cart_target.product_id
                hinted_quantity = cart_target.quantity
            else:
                # Cart intent is clear but the target is not - ask, never
                # guess (requirement 4). No proposal, no discovery.
                trace.capability = "cart_action_ambiguous"
                evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
                trace.response_used_model = False
                trace.finish()
                return TurnResultV2(
                    reply=cart_target.clarifying_question
                    or "Which one did you mean?",
                    state=new_state,
                    evidence=evidence,
                    used_model=False,
                    trace=trace.as_dict(),
                )

    trace.capability = capability.value

    if capability is Capability.COMPARE:
        # Phase 3, first slice - reads `new_state.active_products` (set
        # below, at the end of a DISCOVER turn) to resolve "compare the
        # top two" without needing the model's discarded `ProposedAction`
        # parameters. Both products are always fetched fresh - see
        # compare.py's own docstring.
        reply_text, evidence, products_shown, used_model = await run_compare_turn(
            reasoning=reasoning, adapter=adapter, message=message, state=new_state
        )
        trace.response_used_model = used_model
        trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
        await _record_event(
            connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
            event_type=shopper_events.EventType.COMPARISON_STARTED,
            detail={"product_ids": [p.product_id for p in products_shown]},
        )
        trace.finish()
        return TurnResultV2(
            reply=reply_text,
            state=new_state,
            evidence=evidence,
            used_model=used_model,
            trace=trace.as_dict(),
            products=products_shown,
        )

    if capability is Capability.COMPATIBILITY:
        reply_text, evidence, products_shown, used_model = await run_compatibility_turn(
            reasoning=reasoning, adapter=adapter, message=message, state=new_state
        )
        trace.response_used_model = used_model
        trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
        if products_shown:
            await _record_event(
                connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
                event_type=shopper_events.EventType.PRODUCT_IMPRESSION,
                product_id=products_shown[0].product_id,
                detail={"capability": "compatibility"},
            )
        trace.finish()
        return TurnResultV2(
            reply=reply_text, state=new_state, evidence=evidence,
            used_model=used_model, trace=trace.as_dict(), products=products_shown,
        )

    if capability is Capability.COMPLEMENTARY:
        reply_text, evidence, products_shown, used_model = await run_complementary_turn(
            reasoning=reasoning, adapter=adapter, message=message, state=new_state
        )
        trace.response_used_model = used_model
        trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
        await _record_impressions(
            connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
            product_ids=[p.product_id for p in products_shown],
        )
        trace.finish()
        return TurnResultV2(
            reply=reply_text, state=new_state, evidence=evidence,
            used_model=used_model, trace=trace.as_dict(), products=products_shown,
        )

    if capability is Capability.CART_ACTION:
        # Phase 4, first slice - see cart_proposal.py's own docstring for
        # the full call path and the hard safety rule (this can only ever
        # build a PROPOSE_CART_CHANGE candidate, never a direct mutation).
        # Degrades honestly, without calling decision/gate/execution at
        # all, when this engine deployment did not wire them in - never a
        # silent fallback to "not yet available" that would misrepresent
        # a real capability gap as a routing gap.
        if decision is None or gate is None or execution is None:
            reply_text = (
                "I can't propose cart changes through this conversation "
                "path right now - please use the cart panel directly."
            )
            evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
            trace.finish()
            return TurnResultV2(
                reply=reply_text,
                state=new_state,
                evidence=evidence,
                used_model=False,
                trace=trace.as_dict(),
            )
        reply_text, proposal = await run_cart_change_proposal_turn(
            decision=decision,
            gate=gate,
            execution=execution,
            connection_id=connection_id,
            session_id=session_id,
            cart_id=cart_id,
            message=message,
            state=new_state,
            operation=cart_operation,
            hinted_product_id=hinted_product_id,
            quantity=hinted_quantity,
        )
        evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
        trace.response_used_model = False
        if proposal:
            await _record_event(
                connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
                event_type=shopper_events.EventType.PROPOSAL_CREATED,
                product_id=proposal.get("product_id"),
                detail={"proposal_id": proposal.get("proposal_id"), "change_type": proposal.get("change_type")},
            )
        trace.finish()
        return TurnResultV2(
            reply=reply_text,
            state=new_state,
            evidence=evidence,
            used_model=False,
            trace={**trace.as_dict(), "cart_proposal_id": proposal.get("proposal_id") if proposal else None},
        )

    if capability is Capability.CHECKOUT:
        # Phase 5, first slice - see checkout.py's own docstring. Runs
        # PREPARE_CHECKOUT through the real Decision Engine/Risk Gate/
        # ExecutionService (non-financial, so it completes in this same
        # turn, unlike CART_ACTION's propose-then-approve shape) and
        # replies ONLY from the real returned payload - never claims
        # payment happened, and has no path to /api/chat/pay at all.
        if decision is None or gate is None or execution is None:
            reply_text = (
                "I can't show your checkout total through this "
                "conversation path right now - please use the cart panel "
                "directly."
            )
            evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
            trace.finish()
            return TurnResultV2(
                reply=reply_text,
                state=new_state,
                evidence=evidence,
                used_model=False,
                trace=trace.as_dict(),
            )
        reply_text, payload = await run_checkout_turn(
            decision=decision,
            gate=gate,
            execution=execution,
            connection_id=connection_id,
            session_id=session_id,
            cart_id=cart_id,
            message=message,
        )
        evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
        trace.response_used_model = False
        if payload:
            await _record_event(
                connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
                event_type=shopper_events.EventType.CHECKOUT_STARTED,
                detail={"grand_total": payload.get("grand_total")},
            )
        trace.finish()
        return TurnResultV2(
            reply=reply_text,
            state=new_state,
            evidence=evidence,
            used_model=False,
            trace={
                **trace.as_dict(),
                "checkout_grand_total": payload.get("grand_total") if payload else None,
            },
        )

    if capability is Capability.PAYMENT_RECOVERY:
        # Phase 5 continuation - see payment_recovery.py's own docstring.
        # Proposes all three recovery ActionTypes and lets the real
        # DecisionEngine/RiskGate decide - never executes a financial
        # action itself; FINANCIAL_ALWAYS_HUMAN makes that structurally
        # impossible from this path.
        if decision is None or gate is None or execution is None:
            reply_text = (
                "I can't look into a payment problem through this "
                "conversation path right now - please contact the shop "
                "directly."
            )
            evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
            trace.finish()
            return TurnResultV2(
                reply=reply_text,
                state=new_state,
                evidence=evidence,
                used_model=False,
                trace=trace.as_dict(),
            )
        reply_text, summary = await run_payment_recovery_turn(
            decision=decision,
            gate=gate,
            execution=execution,
            connection_id=connection_id,
            session_id=session_id,
            cart_id=cart_id,
            message=message,
        )
        evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
        trace.response_used_model = False
        await _record_event(
            connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
            event_type=shopper_events.EventType.PAYMENT_RECOVERY_OFFERED,
            detail=summary if isinstance(summary, dict) else {"summary": summary},
        )
        trace.finish()
        return TurnResultV2(
            reply=reply_text,
            state=new_state,
            evidence=evidence,
            used_model=False,
            trace={**trace.as_dict(), "payment_recovery": summary},
        )

    if capability is Capability.ORDER_STATUS:
        # Purchase outcome / post-purchase - see order_status.py's own
        # docstring. Reuses the existing CHECK_ORDER_STATUS dispatch
        # unmodified; never infers purchase success from anything but the
        # real, authoritative order the adapter returns.
        if decision is None or gate is None or execution is None:
            reply_text = (
                "I can't look up an order through this conversation path "
                "right now - please check your account's orders directly."
            )
            evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
            trace.finish()
            return TurnResultV2(
                reply=reply_text,
                state=new_state,
                evidence=evidence,
                used_model=False,
                trace=trace.as_dict(),
            )
        reply_text, payload = await run_order_status_turn(
            decision=decision,
            gate=gate,
            execution=execution,
            connection_id=connection_id,
            session_id=session_id,
            message=message,
            hinted_order_id=result.hinted_order_id,
            owner_keys=owner_keys,
        )
        evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
        trace.response_used_model = False
        await _record_event(
            connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
            event_type=shopper_events.EventType.ORDER_STATUS_CHECKED,
            detail={
                "order_id": payload.get("order_id") if payload else None,
                "found": payload is not None,
            },
        )
        trace.finish()
        return TurnResultV2(
            reply=reply_text,
            state=new_state,
            evidence=evidence,
            used_model=False,
            trace={**trace.as_dict(), "order_id": payload.get("order_id") if payload else None},
        )

    if capability is Capability.CART_STATUS:
        # Cart intelligence / "what's in my cart" - see cart_status.py's
        # own docstring. A real V1 parity gap closed: V1's chat.py has a
        # dedicated CHECK_CART_STATUS branch precisely because this
        # question used to fall through to an unrelated browse answer.
        # Read-only, reuses the existing CHECK_CART_STATUS dispatch
        # unmodified - never a proposal, never a mutation.
        if decision is None or gate is None or execution is None:
            reply_text = (
                "I can't check your cart through this conversation path "
                "right now - please use the cart panel directly."
            )
            evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
            trace.finish()
            return TurnResultV2(
                reply=reply_text,
                state=new_state,
                evidence=evidence,
                used_model=False,
                trace=trace.as_dict(),
            )
        reply_text, payload = await run_cart_status_turn(
            decision=decision,
            gate=gate,
            execution=execution,
            connection_id=connection_id,
            session_id=session_id,
            cart_id=cart_id,
            message=message,
        )
        evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
        trace.response_used_model = False
        trace.finish()
        return TurnResultV2(
            reply=reply_text,
            state=new_state,
            evidence=evidence,
            used_model=False,
            trace={**trace.as_dict(), "cart_item_count": (payload or {}).get("item_count")},
        )

    if capability is not Capability.DISCOVER:
        reply_text = _UNIMPLEMENTED_CAPABILITY_REPLIES[capability]
        evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
        trace.response_used_model = False
        trace.finish()
        return TurnResultV2(
            reply=reply_text,
            state=new_state,
            evidence=evidence,
            used_model=False,
            trace=trace.as_dict(),
        )

    # 6. Retrieval + evidence, via the Phase 1/2 production pipeline -
    #    wires the Stage 2A typed contracts into this live path and closes
    #    Stage 2A Findings 4 and 5 (see pipeline.py's own docstring).
    evidence, products, _intent = await run_discovery_turn(
        reasoning=reasoning,
        adapter=adapter,
        session_id=session_id,
        connection_id=connection_id,
        message=message,
        delta_query=delta.search_query,
        shopping_state=new_state.as_prompt_dict(),
        memory_facts=memory_facts,
        trace=trace,
    )

    # 7. Response - generated ONLY from evidence/products/state. Never from
    #    the pre-fetch understanding reply, which no longer exists by this
    #    point (it was discarded in step 3).
    reply_text, used_model = await respond_(
        reasoning=reasoning,
        message=message,
        evidence=evidence,
        products=products,
        state=new_state,
    )
    trace.response_used_model = used_model

    # Persist what was actually shown as "currently under consideration" -
    # `ShoppingStateV2.active_products`'s own documented purpose (see
    # state.py/models.py) - so a LATER "compare the top two" (Capability.COMPARE
    # above) has something concrete to resolve, without needing the model's
    # discarded action parameters. Only real, verified products; never the
    # full candidate set.
    verified_ids = [e.product_id for e in evidence.products if e.relevance_verified]
    if verified_ids:
        await shopping_state_store.merge(session_id, connection_id, active_products=verified_ids)
        new_state.active_products = verified_ids
        await _record_impressions(
            connection_id=connection_id, session_id=session_id, shopper_id=shopper_id,
            product_ids=verified_ids,
        )

    trace.finish()

    shown = [p for p in products if p.product_id in set(verified_ids)]
    return TurnResultV2(
        reply=reply_text,
        state=new_state,
        evidence=evidence,
        used_model=used_model,
        trace=trace.as_dict(),
        products=shown,
    )
