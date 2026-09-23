"""ShopperTurnGraph - the LangGraph migration of the Shopper V2 turn
orchestrator, per the approved architecture review (2026-09).

Why: `orchestrator.py::run_turn` had grown into one ~700-line function with
capability dispatch as a flat sequence of `if capability is X` blocks, whose
correctness depended on *implicit ordering* between hand-added bypass
guards. That shape produced three real, reproduced bugs in one session: an
explicit `friction` signal silently swallowed by the mission-detection
heuristic, then by the cart-intent resolver, each requiring a NEW
`if friction is None and ...` guard bolted onto an unrelated check. This
module replaces that shape with an explicit graph: every routing decision
is a conditional edge, a pure function over typed state, so "does X always
run before Y" is a graph-structure question, not a code-review question.

**This is a control-flow migration, not a logic rewrite.** Every node below
calls the exact same, unmodified underlying function orchestrator.py already
called at that point (`understand`, `apply_delta`, `run_mission_turn`,
`run_tradeoff_turn`, `resolve_cart_target`, `run_cart_change_proposal_turn`,
`run_discovery_turn`, `respond`, etc.) - no business logic changes, no new
LLM call, no new adapter call. Decision/Risk/Execution/Adapter are reached
exactly as before: opaque objects passed through to `cart_proposal.py`/
`checkout.py`/`payment_recovery.py`, never touched by anything graph-related.

No checkpointer is configured - this graph is compiled once at import time
and invoked per turn entirely in-process, exactly matching the previous
function-call model. No new persistence, no new state store: the graph's
own state is a thin typed wrapper over the same `ShoppingStateV2`/
`session_turns`/`ShopperMemoryFact` this package already used.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from shared.models import Product

from engine.db import shopping_state as shopping_state_store
from engine.execution import events as shopper_events
from engine.reasoning.service import ReasoningService

from .capability import (
    Capability,
    looks_like_cart_intelligence_request,
    looks_like_comparison_request,
    looks_like_historical_reference,
    looks_like_recommendation_request,
    route,
)
from .cart_proposal import run_cart_change_proposal_turn
from .checkout import run_checkout_turn
from .compare import run_compare_turn
from .compatibility import run_compatibility_turn, run_complementary_turn
from .context import build_turn_context
from .cart_status import run_cart_status_turn
from .cart_intelligence_v2 import run_cart_intelligence_turn
from . import friction_recovery
from .friction_recovery import run_friction_recovery_turn
from .conversational import run_conversational_turn
from .order_status import run_order_status_turn
from .payment_recovery import run_payment_recovery_turn
from .evidence import TurnEvidence
from .intent_resolution import resolve_cart_target
from .missions import run_mission_turn
from .pipeline import run_discovery_turn
from .respond import respond as respond_
from .state import ShoppingStateV2, apply_delta, load, recent_relevant_product_ids
from .tracing import TurnTrace
from .tradeoff import run_tradeoff_turn
from .understanding import understand

logger = logging.getLogger(__name__)

#: Bounded lookback for `ShoppingStateV2.recent_product_groups` - the
#: rolling in-view ledger. Unchanged from orchestrator.py's own constant.
_LEDGER_MAX_GROUPS = 5


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


async def _record_event(
    *,
    connection_id: str,
    session_id: str,
    shopper_id: str | None,
    event_type: str,
    product_id: str | None = None,
    detail: dict | None = None,
) -> None:
    """Best-effort, never allowed to fail a turn - unchanged from
    orchestrator.py's own helper."""
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
    """Best-effort, same discipline as `_record_event` - unchanged."""
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


class ShopperTurnState(TypedDict, total=False):
    """Typed graph state - a thin wrapper over the same real objects
    orchestrator.py already threaded through its call chain. Arbitrary
    Python objects (adapter, reasoning, decision/gate/execution) are held
    by reference, exactly as before; no serialization, no checkpointer, no
    new persistence. See module docstring."""

    # Turn inputs - immutable for the life of the turn.
    reasoning: ReasoningService
    adapter: Any
    session_id: str
    connection_id: str
    message: str
    friction: str | None
    memory_facts: list[dict] | None
    cart_id: str | None
    decision: Any
    gate: Any
    execution: Any
    owner_keys: list[str] | None
    shopper_id: str | None

    # Working state, populated as nodes run.
    trace: TurnTrace
    history: list[str] | None
    current_state: ShoppingStateV2
    delta: Any  # StateDelta
    new_state: ShoppingStateV2
    hinted_action_type: str | None
    hinted_order_id: str | None
    understanding_hinted_product_id: str | None
    message_intent: str | None
    conversational_reply: str | None

    capability: Capability
    cart_operation: str
    hinted_product_id: str | None
    hinted_product_ids: list[str] | None
    hinted_quantity: int | None

    products: list[Product]
    evidence: TurnEvidence
    reply_text: str
    used_model: bool

    # Terminal output - set by whichever node ends the turn.
    result: TurnResultV2


# --------------------------------------------------------------------------
# Node 1-2: load context, understand
# --------------------------------------------------------------------------

async def node_load_context(state: ShopperTurnState) -> dict:
    current_state = await load(state["session_id"], state["connection_id"])
    turn_context = await build_turn_context(state["session_id"], state["connection_id"])
    trace = TurnTrace(session_id=state["session_id"], connection_id=state["connection_id"])
    return {
        "current_state": current_state,
        "history": turn_context.history,
        "trace": trace,
    }


async def node_understand(state: ShopperTurnState) -> dict:
    result = await understand(
        reasoning=state["reasoning"],
        message=state["message"],
        history=state["history"],
        shopping_state=state["current_state"].as_prompt_dict(),
        memory_facts=state.get("memory_facts"),
    )
    trace = state["trace"]
    trace.understanding_used_model = result.used_model
    trace.understanding_model_name = result.model_name
    return {
        "delta": result.delta,
        "hinted_action_type": result.hinted_action_type,
        "understanding_hinted_product_id": result.hinted_product_id,
        "hinted_order_id": result.hinted_order_id,
        "message_intent": result.message_intent,
        "conversational_reply": result.conversational_reply,
        "trace": trace,
    }


async def node_apply_delta(state: ShopperTurnState) -> dict:
    new_state = await apply_delta(state["current_state"], state["delta"])
    trace = state["trace"]
    trace.state_version = new_state.version
    return {"new_state": new_state, "trace": trace}


# --------------------------------------------------------------------------
# Conditional edges after apply_delta: mission -> tradeoff -> capability
# routing. Each is a pure function over state, mirroring the exact
# conditions orchestrator.py's sequential `if` blocks used to check - now
# structural graph edges instead of implicit fall-through order, which is
# the actual fix for the friction-bypass bug class found this session.
# --------------------------------------------------------------------------

def edge_after_apply_delta(state: ShopperTurnState) -> str:
    new_state = state["new_state"]
    friction = state.get("friction")
    if friction is None and state.get("reasoning") is not None and (
        new_state.mission_items
        or any(sep in state["message"].lower() for sep in (",", " and ", " & "))
    ):
        return "mission"
    return edge_after_mission_check(state)


def edge_after_mission_check(state: ShopperTurnState) -> str:
    friction = state.get("friction")
    delta = state["delta"]
    new_state = state["new_state"]
    if friction is None and delta.priorities_add and new_state.active_products:
        return "tradeoff"
    return "route_capability"


async def node_mission(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    (
        mission_reply, mission_products, mission_state, mission_handled, mission_proposal_ids,
    ) = await run_mission_turn(
        reasoning=state["reasoning"],
        adapter=state["adapter"],
        message=state["message"],
        history=state["history"],
        state=state["new_state"],
        decision=state.get("decision"),
        gate=state.get("gate"),
        execution=state.get("execution"),
        connection_id=state["connection_id"],
        session_id=state["session_id"],
        cart_id=state.get("cart_id"),
        memory_facts=state.get("memory_facts"),
    )
    if not mission_handled:
        # Falls through to exactly where the mission-check would have been
        # skipped - same as orchestrator.py's original "if not handled,
        # keep going" shape.
        return {}
    new_state = mission_state
    trace.capability = "mission"
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    await _record_event(
        connection_id=state["connection_id"], session_id=state["session_id"],
        shopper_id=state.get("shopper_id"),
        event_type=shopper_events.EventType.MISSION_ITEM_TRACKED,
        detail={"item_count": len(new_state.mission_items)},
    )
    if mission_proposal_ids:
        await _record_event(
            connection_id=state["connection_id"], session_id=state["session_id"],
            shopper_id=state.get("shopper_id"),
            event_type=shopper_events.EventType.BASKET_PROPOSED,
            detail={"proposal_ids": mission_proposal_ids},
        )

    # Live-reproduced bug (conversational-flow review): a mission turn
    # never went through `node_finalize`, so `active_products`/
    # `recent_product_groups` - the SAME ledger `compare.py`/`tradeoff.py`/
    # `intent_resolution.py` already read via `recent_relevant_product_ids`
    # - never learned about anything a mission tracked. "Show me shoes and
    # socks" (mission mode) followed by "compare both" or "add both to my
    # cart" found NOTHING, because those modules only ever look at the
    # single-item ledger, which a mission turn had never written to - two
    # real tracking systems that never talked to each other. Fixed by
    # writing the SAME ledger here, using each item's own top real,
    # already-verified candidate as its "currently shown" representative -
    # exactly what a shopper reading the reply is actually looking at, and
    # exactly the same shape `node_finalize` already produces for ordinary
    # discovery, so every module that already reads this ledger correctly
    # (never re-implemented) starts working for missions too, for free.
    verified_ids = [item.candidates[0] for item in new_state.mission_items if item.candidates]
    if verified_ids:
        groups = list(new_state.recent_product_groups)
        if not groups or groups[-1] != verified_ids:
            groups.append(verified_ids)
        groups = groups[-_LEDGER_MAX_GROUPS:]
        await shopping_state_store.merge(
            state["session_id"], state["connection_id"],
            active_products=verified_ids, recent_product_groups=groups,
        )
        new_state.active_products = verified_ids
        new_state.recent_product_groups = groups

    trace.finish()
    result = TurnResultV2(
        reply=mission_reply,
        state=new_state,
        evidence=evidence,
        used_model=False,
        trace={**trace.as_dict(), "mission_proposal_ids": mission_proposal_ids},
        products=mission_products,
    )
    return {"new_state": new_state, "trace": trace, "result": result}


def edge_after_mission(state: ShopperTurnState) -> str:
    if "result" in state and state["result"] is not None:
        return END
    return edge_after_mission_check(state)


async def node_tradeoff(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    new_state = state["new_state"]
    tradeoff_reply, tradeoff_evidence, tradeoff_products, reordered_ids = await run_tradeoff_turn(
        adapter=state["adapter"], message=state["message"], state=new_state,
    )
    trace.capability = "trade_off"
    trace.response_used_model = False
    if reordered_ids:
        await shopping_state_store.merge(
            state["session_id"], state["connection_id"], active_products=reordered_ids
        )
        new_state.active_products = reordered_ids
    await _record_event(
        connection_id=state["connection_id"], session_id=state["session_id"],
        shopper_id=state.get("shopper_id"),
        event_type=shopper_events.EventType.TRADE_OFF_APPLIED,
        detail={"priorities": list(new_state.priorities)},
    )
    trace.finish()
    result = TurnResultV2(
        reply=tradeoff_reply,
        state=new_state,
        evidence=tradeoff_evidence,
        used_model=False,
        trace=trace.as_dict(),
        products=tradeoff_products,
    )
    return {"new_state": new_state, "trace": trace, "result": result}


# --------------------------------------------------------------------------
# Capability routing + reference resolution
# --------------------------------------------------------------------------

async def node_route_capability(state: ShopperTurnState) -> dict:
    friction = state.get("friction")
    if friction == "PAYMENT_DECLINED":
        capability = Capability.PAYMENT_RECOVERY
    elif friction in (friction_recovery.DEAD_SEARCH, friction_recovery.PROMOTION_FAILED):
        capability = Capability.FRICTION_RECOVERY
    else:
        capability = route(state["hinted_action_type"], state.get("message_intent"))
        # Deterministic comparison backstop - see capability.py's own
        # docstring on the live-reproduced primary-classifier variance
        # this closes. Only overrides a "soft" default (DISCOVER/
        # CONVERSATIONAL, i.e. the model itself was not decisive about
        # anything else) and only when there are genuinely 2+ real,
        # already-tracked products to compare - never invents a
        # comparison out of nothing, never overrides an already-specific
        # hint like CART_ACTION/CHECKOUT.
        # Conversational-flow review fix (Issue #2): a genuine historical
        # reference ("what was the cheapest shoe you showed me earlier")
        # has neither a comparison nor a recommendation word in it - live-
        # reproduced via Playwright, this fell through to an ordinary new
        # discovery search (re-querying the catalog from scratch) because
        # `looks_like_historical_reference` was checked inside
        # `run_compare_turn` but that turn is only ever REACHED when
        # routing already picked COMPARE - it never fired here at all.
        if (
            capability in (Capability.DISCOVER, Capability.CONVERSATIONAL)
            and (
                looks_like_comparison_request(state["message"])
                or looks_like_recommendation_request(state["message"])
                or looks_like_historical_reference(state["message"])
            )
            and len(recent_relevant_product_ids(state["new_state"], minimum=2)) >= 2
        ):
            capability = Capability.COMPARE
        # Deterministic cart-intelligence backstop - see capability.py's
        # own docstring on the live-reproduced gap this closes ("My cart
        # is over 10000..." fell through to a literal catalog search for
        # those words instead of analyzing the real cart). Only overrides
        # the same "soft" defaults, and only when a cart genuinely exists
        # for this turn - never invents cart analysis out of nothing.
        elif (
            capability in (Capability.DISCOVER, Capability.CONVERSATIONAL)
            and looks_like_cart_intelligence_request(state["message"])
            and state.get("cart_id")
        ):
            capability = Capability.CART_INTELLIGENCE
    return {
        "capability": capability,
        "hinted_product_id": state.get("understanding_hinted_product_id"),
        "hinted_product_ids": None,
        "hinted_quantity": None,
        "cart_operation": "add",
    }


def edge_after_route_capability(state: ShopperTurnState) -> str:
    # An explicit friction routing decision is authoritative and must never
    # be silently overridden by cart-intent resolution misreading a
    # synthesized friction sentence - the second of the two real bugs this
    # session found in the old flat-guard shape.
    if (
        state.get("friction") is None
        and state.get("decision") is not None
        and state.get("gate") is not None
        and state.get("execution") is not None
    ):
        return "reference_resolution"
    return "capability_dispatch"


async def node_reference_resolution(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    cart_target = await resolve_cart_target(
        reasoning=state["reasoning"],
        adapter=state["adapter"],
        message=state["message"],
        history=state["history"],
        state=state["new_state"],
        cart_id=state.get("cart_id"),
    )
    trace.cart_intent_checked = True
    trace.cart_intent_detected = cart_target.is_cart_intent
    if not cart_target.is_cart_intent:
        return {"trace": trace}

    if cart_target.product_ids:
        return {
            "trace": trace,
            "capability": Capability.CART_ACTION,
            "cart_operation": cart_target.operation,
            "hinted_product_ids": cart_target.product_ids,
            "hinted_quantity": cart_target.quantity,
        }
    if cart_target.operation == "clear" or cart_target.product_id:
        return {
            "trace": trace,
            "capability": Capability.CART_ACTION,
            "cart_operation": cart_target.operation,
            "hinted_product_id": cart_target.product_id,
            "hinted_quantity": cart_target.quantity,
        }

    # Cart intent is clear but the target is not - ask, never guess.
    trace.capability = "cart_action_ambiguous"
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=cart_target.clarifying_question or "Which one did you mean?",
        state=state["new_state"],
        evidence=evidence,
        used_model=False,
        trace=trace.as_dict(),
    )
    return {"trace": trace, "result": result}


def edge_after_reference_resolution(state: ShopperTurnState) -> str:
    if "result" in state and state["result"] is not None:
        return END
    return "capability_dispatch"


# --------------------------------------------------------------------------
# Capability dispatch - one node per capability, each a faithful port of
# orchestrator.py's own inline branch. Every node here is terminal (sets
# `result` and the graph edges straight to END) except `discovery`, which
# continues to `respond`.
# --------------------------------------------------------------------------

_NO_PIPELINE_REPLIES: dict[Capability, str] = {
    Capability.CART_ACTION: (
        "I can't propose cart changes through this conversation path right "
        "now - please use the cart panel directly."
    ),
    Capability.CHECKOUT: (
        "I can't show your checkout total through this conversation path "
        "right now - please use the cart panel directly."
    ),
    Capability.PAYMENT_RECOVERY: (
        "I can't look into a payment problem through this conversation "
        "path right now - please contact the shop directly."
    ),
    Capability.ORDER_STATUS: (
        "I can't look up an order through this conversation path right "
        "now - please check your account's orders directly."
    ),
    Capability.CART_STATUS: (
        "I can't check your cart through this conversation path right "
        "now - please use the cart panel directly."
    ),
}


def _no_pipeline_result(state: ShopperTurnState, capability: Capability) -> dict:
    trace = state["trace"]
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.finish()
    result = TurnResultV2(
        reply=_NO_PIPELINE_REPLIES[capability],
        state=state["new_state"],
        evidence=evidence,
        used_model=False,
        trace=trace.as_dict(),
    )
    return {"trace": trace, "result": result}


def _requires_pipeline(capability: Capability):
    """Node decorator: the identical `if decision/gate/execution is None:
    degrade honestly` check used to be duplicated inline in 5 separate
    terminal nodes (cart_action, checkout, payment_recovery, order_status,
    cart_status) - exactly the kind of copy-pasted-guard shape this
    project's own CLAUDE.md calls out as a recurring bug class (grep every
    caller of the same pattern rather than patch one instance). One
    decorator, one place the check lives, applied to each node that needs
    it - a real reduction, not just a cosmetic one, in the same spirit as
    the Batch 2 review's "don't recreate the monolithic problem inside
    graph.py" instruction."""
    def decorator(fn):
        async def wrapped(state: ShopperTurnState) -> dict:
            if (
                state.get("decision") is None
                or state.get("gate") is None
                or state.get("execution") is None
            ):
                return _no_pipeline_result(state, capability)
            return await fn(state)
        wrapped.__name__ = fn.__name__
        return wrapped
    return decorator


def edge_capability_dispatch(state: ShopperTurnState) -> str:
    state["trace"].capability = state["capability"].value
    capability = state["capability"]
    mapping = {
        Capability.COMPARE: "compare",
        Capability.COMPATIBILITY: "compatibility",
        Capability.COMPLEMENTARY: "complementary",
        Capability.CART_ACTION: "cart_action",
        Capability.CHECKOUT: "checkout",
        Capability.PAYMENT_RECOVERY: "payment_recovery",
        Capability.ORDER_STATUS: "order_status",
        Capability.CART_STATUS: "cart_status",
        Capability.CART_INTELLIGENCE: "cart_intelligence",
        Capability.FRICTION_RECOVERY: "friction_recovery",
        Capability.CONVERSATIONAL: "conversational",
        Capability.DISCOVER: "discovery",
    }
    return mapping.get(capability, "unimplemented")


async def node_compare(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, evidence, products_shown, used_model = await run_compare_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], message=state["message"],
        state=state["new_state"],
    )
    trace.response_used_model = used_model
    trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
    await _record_event(
        connection_id=state["connection_id"], session_id=state["session_id"],
        shopper_id=state.get("shopper_id"),
        event_type=shopper_events.EventType.COMPARISON_STARTED,
        detail={"product_ids": [p.product_id for p in products_shown]},
    )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence,
        used_model=used_model, trace=trace.as_dict(), products=products_shown,
    )
    return {"trace": trace, "result": result}


async def node_compatibility(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, evidence, products_shown, used_model = await run_compatibility_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], message=state["message"],
        state=state["new_state"],
    )
    trace.response_used_model = used_model
    trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
    if products_shown:
        await _record_event(
            connection_id=state["connection_id"], session_id=state["session_id"],
            shopper_id=state.get("shopper_id"),
            event_type=shopper_events.EventType.PRODUCT_IMPRESSION,
            product_id=products_shown[0].product_id,
            detail={"capability": "compatibility"},
        )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence,
        used_model=used_model, trace=trace.as_dict(), products=products_shown,
    )
    return {"trace": trace, "result": result}


async def node_complementary(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, evidence, products_shown, used_model = await run_complementary_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], message=state["message"],
        state=state["new_state"],
    )
    trace.response_used_model = used_model
    trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
    await _record_impressions(
        connection_id=state["connection_id"], session_id=state["session_id"],
        shopper_id=state.get("shopper_id"),
        product_ids=[p.product_id for p in products_shown],
    )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence,
        used_model=used_model, trace=trace.as_dict(), products=products_shown,
    )
    return {"trace": trace, "result": result}


@_requires_pipeline(Capability.CART_ACTION)
async def node_cart_action(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    hinted_product_ids = state.get("hinted_product_ids")
    if hinted_product_ids:
        reply_parts: list[str] = []
        proposal_ids: list[str] = []
        for pid in hinted_product_ids:
            item_reply, item_proposal = await run_cart_change_proposal_turn(
                decision=state["decision"], gate=state["gate"], execution=state["execution"],
                connection_id=state["connection_id"], session_id=state["session_id"],
                cart_id=state.get("cart_id"), message=state["message"], state=state["new_state"],
                operation="add", hinted_product_id=pid, quantity=state.get("hinted_quantity"),
            )
            reply_parts.append(item_reply)
            if item_proposal:
                proposal_ids.append(item_proposal.get("proposal_id"))
                await _record_event(
                    connection_id=state["connection_id"], session_id=state["session_id"],
                    shopper_id=state.get("shopper_id"),
                    event_type=shopper_events.EventType.PROPOSAL_CREATED,
                    product_id=item_proposal.get("product_id"),
                    detail={
                        "proposal_id": item_proposal.get("proposal_id"),
                        "change_type": item_proposal.get("change_type"),
                        "source": "multi_item_add",
                    },
                )
        evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
        trace.response_used_model = False
        trace.finish()
        reply_text = (
            f"I've put together {len(proposal_ids)} suggestions for you to approve: "
            + " ".join(reply_parts)
            if proposal_ids
            else "I couldn't propose those - please try one at a time."
        )
        result = TurnResultV2(
            reply=reply_text, state=state["new_state"], evidence=evidence,
            used_model=False, trace={**trace.as_dict(), "cart_proposal_ids": proposal_ids},
        )
        return {"trace": trace, "result": result}

    reply_text, proposal = await run_cart_change_proposal_turn(
        decision=state["decision"], gate=state["gate"], execution=state["execution"],
        connection_id=state["connection_id"], session_id=state["session_id"],
        cart_id=state.get("cart_id"), message=state["message"], state=state["new_state"],
        operation=state.get("cart_operation", "add"), hinted_product_id=state.get("hinted_product_id"),
        quantity=state.get("hinted_quantity"),
    )
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    if proposal:
        await _record_event(
            connection_id=state["connection_id"], session_id=state["session_id"],
            shopper_id=state.get("shopper_id"),
            event_type=shopper_events.EventType.PROPOSAL_CREATED,
            product_id=proposal.get("product_id"),
            detail={"proposal_id": proposal.get("proposal_id"), "change_type": proposal.get("change_type")},
        )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=False,
        trace={**trace.as_dict(), "cart_proposal_id": proposal.get("proposal_id") if proposal else None},
    )
    return {"trace": trace, "result": result}


@_requires_pipeline(Capability.CHECKOUT)
async def node_checkout(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, payload = await run_checkout_turn(
        decision=state["decision"], gate=state["gate"], execution=state["execution"],
        connection_id=state["connection_id"], session_id=state["session_id"],
        cart_id=state.get("cart_id"), message=state["message"],
    )
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    if payload:
        await _record_event(
            connection_id=state["connection_id"], session_id=state["session_id"],
            shopper_id=state.get("shopper_id"),
            event_type=shopper_events.EventType.CHECKOUT_STARTED,
            detail={"grand_total": payload.get("grand_total")},
        )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=False,
        trace={
            **trace.as_dict(),
            "checkout_grand_total": payload.get("grand_total") if payload else None,
            "checkout_payload": payload,
        },
    )
    return {"trace": trace, "result": result}


@_requires_pipeline(Capability.PAYMENT_RECOVERY)
async def node_payment_recovery(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, summary = await run_payment_recovery_turn(
        decision=state["decision"], gate=state["gate"], execution=state["execution"],
        connection_id=state["connection_id"], session_id=state["session_id"],
        cart_id=state.get("cart_id"), message=state["message"],
    )
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    await _record_event(
        connection_id=state["connection_id"], session_id=state["session_id"],
        shopper_id=state.get("shopper_id"),
        event_type=shopper_events.EventType.PAYMENT_RECOVERY_OFFERED,
        detail=summary if isinstance(summary, dict) else {"summary": summary},
    )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=False,
        trace={**trace.as_dict(), "payment_recovery": summary},
    )
    return {"trace": trace, "result": result}


@_requires_pipeline(Capability.ORDER_STATUS)
async def node_order_status(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, payload = await run_order_status_turn(
        decision=state["decision"], gate=state["gate"], execution=state["execution"],
        connection_id=state["connection_id"], session_id=state["session_id"],
        message=state["message"], hinted_order_id=state.get("hinted_order_id"),
        owner_keys=state.get("owner_keys"),
    )
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    await _record_event(
        connection_id=state["connection_id"], session_id=state["session_id"],
        shopper_id=state.get("shopper_id"),
        event_type=shopper_events.EventType.ORDER_STATUS_CHECKED,
        detail={"order_id": payload.get("order_id") if payload else None, "found": payload is not None},
    )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=False,
        trace={**trace.as_dict(), "order_id": payload.get("order_id") if payload else None},
    )
    return {"trace": trace, "result": result}


@_requires_pipeline(Capability.CART_STATUS)
async def node_cart_status(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, payload = await run_cart_status_turn(
        decision=state["decision"], gate=state["gate"], execution=state["execution"],
        connection_id=state["connection_id"], session_id=state["session_id"],
        cart_id=state.get("cart_id"), message=state["message"],
    )
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=False,
        trace={**trace.as_dict(), "cart_item_count": (payload or {}).get("item_count")},
    )
    return {"trace": trace, "result": result}


@_requires_pipeline(Capability.CART_INTELLIGENCE)
async def node_cart_intelligence(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, payload = await run_cart_intelligence_turn(
        decision=state["decision"], gate=state["gate"], execution=state["execution"],
        connection_id=state["connection_id"], session_id=state["session_id"],
        cart_id=state.get("cart_id"), message=state["message"],
    )
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=False,
        trace={
            **trace.as_dict(),
            "cart_intelligence_findings": len((payload or {}).get("findings") or []),
        },
    )
    return {"trace": trace, "result": result}


async def node_friction_recovery(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, evidence = await run_friction_recovery_turn(
        friction=state.get("friction") or "", adapter=state["adapter"], state=state["new_state"],
    )
    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=False,
        trace=trace.as_dict(),
    )
    return {"trace": trace, "result": result}


async def node_conversational(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text, used_model = run_conversational_turn(
        conversational_reply=state.get("conversational_reply"),
        used_model=bool(state.get("conversational_reply")),
        state=state["new_state"],
    )
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = used_model
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=used_model,
        trace=trace.as_dict(),
    )
    return {"trace": trace, "result": result}


async def node_unimplemented(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    reply_text = _UNIMPLEMENTED_CAPABILITY_REPLIES[state["capability"]]
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence, used_model=False,
        trace=trace.as_dict(),
    )
    return {"trace": trace, "result": result}


# --------------------------------------------------------------------------
# Discovery + response grounding + measurement (the only non-terminal
# dispatch branch - continues to respond/finalize).
# --------------------------------------------------------------------------

async def node_discovery(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    delta = state["delta"]
    new_state = state["new_state"]
    # SearchIntent, genuinely retrieval-driving: a deterministic signal from
    # THIS turn's real StateDelta (never a guess at word content) that this
    # turn stated a new budget with no new goal of its own.
    constraint_only_turn = delta.budget_amount is not None and not delta.goal

    evidence, products, _intent = await run_discovery_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], session_id=state["session_id"],
        connection_id=state["connection_id"], message=state["message"],
        delta_query=delta.search_query, constraint_only_turn=constraint_only_turn,
        shopping_state=new_state.as_prompt_dict(), memory_facts=state.get("memory_facts"),
        trace=trace,
    )
    return {"trace": trace, "evidence": evidence, "products": products}


async def node_respond(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    evidence = state["evidence"]
    products = state["products"]
    new_state = state["new_state"]
    reply_text, used_model = await respond_(
        reasoning=state["reasoning"], message=state["message"], evidence=evidence,
        products=products, state=new_state, memory_facts=state.get("memory_facts"),
    )
    trace.response_used_model = used_model
    return {"trace": trace, "reply_text": reply_text, "used_model": used_model}


async def node_finalize(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    evidence = state["evidence"]
    products = state["products"]
    new_state = state["new_state"]
    session_id = state["session_id"]
    connection_id = state["connection_id"]

    verified_ids = [e.product_id for e in evidence.products if e.relevance_verified]
    if verified_ids:
        groups = list(new_state.recent_product_groups)
        if not groups or groups[-1] != verified_ids:
            groups.append(verified_ids)
        groups = groups[-_LEDGER_MAX_GROUPS:]
        await shopping_state_store.merge(
            session_id, connection_id, active_products=verified_ids, recent_product_groups=groups,
        )
        new_state.active_products = verified_ids
        new_state.recent_product_groups = groups
        await _record_impressions(
            connection_id=connection_id, session_id=session_id, shopper_id=state.get("shopper_id"),
            product_ids=verified_ids,
        )

    trace.finish()
    shown = [p for p in products if p.product_id in set(verified_ids)]
    result = TurnResultV2(
        reply=state["reply_text"],
        state=new_state,
        evidence=evidence,
        used_model=state["used_model"],
        trace=trace.as_dict(),
        products=shown,
    )
    return {"trace": trace, "new_state": new_state, "result": result}


# --------------------------------------------------------------------------
# Graph assembly - compiled once at import time.
# --------------------------------------------------------------------------

def _build_graph():
    g = StateGraph(ShopperTurnState)

    g.add_node("load_context", node_load_context)
    g.add_node("understand", node_understand)
    g.add_node("apply_delta", node_apply_delta)
    g.add_node("mission", node_mission)
    g.add_node("tradeoff", node_tradeoff)
    g.add_node("route_capability", node_route_capability)
    g.add_node("reference_resolution", node_reference_resolution)

    g.add_node("compare", node_compare)
    g.add_node("compatibility", node_compatibility)
    g.add_node("complementary", node_complementary)
    g.add_node("cart_action", node_cart_action)
    g.add_node("checkout", node_checkout)
    g.add_node("payment_recovery", node_payment_recovery)
    g.add_node("order_status", node_order_status)
    g.add_node("cart_status", node_cart_status)
    g.add_node("cart_intelligence", node_cart_intelligence)
    g.add_node("friction_recovery", node_friction_recovery)
    g.add_node("conversational", node_conversational)
    g.add_node("unimplemented", node_unimplemented)
    g.add_node("discovery", node_discovery)
    g.add_node("respond", node_respond)
    g.add_node("finalize", node_finalize)

    g.set_entry_point("load_context")
    g.add_edge("load_context", "understand")
    g.add_edge("understand", "apply_delta")

    g.add_conditional_edges(
        "apply_delta", edge_after_apply_delta,
        {"mission": "mission", "tradeoff": "tradeoff", "route_capability": "route_capability"},
    )
    g.add_conditional_edges(
        "mission", edge_after_mission,
        {END: END, "tradeoff": "tradeoff", "route_capability": "route_capability"},
    )
    g.add_edge("tradeoff", END)

    g.add_conditional_edges(
        "route_capability", edge_after_route_capability,
        {"reference_resolution": "reference_resolution", "capability_dispatch": "capability_dispatch_router"},
    )
    g.add_conditional_edges(
        "reference_resolution", edge_after_reference_resolution,
        {END: END, "capability_dispatch": "capability_dispatch_router"},
    )

    # `capability_dispatch_router` is a virtual node (no-op pass-through)
    # so both entry paths (with/without reference resolution) converge on
    # one conditional-edge dispatch, matching orchestrator.py's original
    # single dispatch point.
    g.add_node("capability_dispatch_router", lambda state: {})
    g.add_conditional_edges(
        "capability_dispatch_router", edge_capability_dispatch,
        {
            "compare": "compare",
            "compatibility": "compatibility",
            "complementary": "complementary",
            "cart_action": "cart_action",
            "checkout": "checkout",
            "payment_recovery": "payment_recovery",
            "order_status": "order_status",
            "cart_status": "cart_status",
            "cart_intelligence": "cart_intelligence",
            "friction_recovery": "friction_recovery",
            "conversational": "conversational",
            "unimplemented": "unimplemented",
            "discovery": "discovery",
        },
    )

    for terminal in (
        "compare", "compatibility", "complementary", "cart_action", "checkout",
        "payment_recovery", "order_status", "cart_status", "cart_intelligence",
        "friction_recovery", "conversational", "unimplemented",
    ):
        g.add_edge(terminal, END)

    g.add_edge("discovery", "respond")
    g.add_edge("respond", "finalize")
    g.add_edge("finalize", END)

    return g.compile()


_COMPILED_GRAPH = _build_graph()


async def run_turn_via_graph(
    *,
    reasoning: ReasoningService,
    adapter,
    session_id: str,
    connection_id: str,
    message: str,
    friction: str | None = None,
    memory_facts: list[dict] | None = None,
    catalog_sample: list | None = None,
    cart_id: str | None = None,
    decision=None,
    gate=None,
    execution=None,
    owner_keys: list[str] | None = None,
    shopper_id: str | None = None,
) -> TurnResultV2:
    """Run one V2 turn through the compiled graph. Same signature and
    return type as the pre-graph `orchestrator.run_turn` - `catalog_sample`
    is accepted, unused, for signature compatibility (it was already
    unused in the pre-graph implementation)."""
    initial: ShopperTurnState = {
        "reasoning": reasoning,
        "adapter": adapter,
        "session_id": session_id,
        "connection_id": connection_id,
        "message": message,
        "friction": friction,
        "memory_facts": memory_facts,
        "cart_id": cart_id,
        "decision": decision,
        "gate": gate,
        "execution": execution,
        "owner_keys": owner_keys,
        "shopper_id": shopper_id,
    }
    final_state = await _COMPILED_GRAPH.ainvoke(initial)
    return final_state["result"]
