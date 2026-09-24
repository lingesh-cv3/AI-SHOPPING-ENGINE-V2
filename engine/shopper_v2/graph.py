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

from engine.db import memory_facts as memory_facts_store
from engine.db import shopping_state as shopping_state_store
from engine.execution import events as shopper_events
from engine.reasoning.service import ReasoningService

from .capability import (
    SOFT_CAPABILITIES,
    Capability,
    looks_like_cart_intelligence_request,
    looks_like_reference,
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
from . import fulfillment
from .friction_recovery import run_friction_recovery_turn
from .conversational import run_conversational_turn
from .order_status import run_order_status_turn
from .payment_recovery import run_payment_recovery_turn
from .evidence import TurnEvidence, already_verified_evidence
from .intent_resolution import resolve_cart_target
from .missions import run_mission_turn
from .pipeline import run_discovery_turn
from .reference_resolution import parse_query_intent
from engine.execution import ranking
from engine.execution.relevance import best_matches_with_score
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
    #: A tapped variant-option button, when the frontend has one to send -
    #: see `fulfillment.py::_match_answer`'s own docstring on why a tap is
    #: trusted here exactly like `chosen_variant` elsewhere in this
    #: codebase. `None` for an ordinary turn; the exact-text fallback in
    #: the same function covers a shopper who types the option instead.
    selected_variant_id: str | None

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
    #: A real, deterministic reply already computed in `node_discovery`
    #: (a genuine superlative - "the cheapest one is X, at Y" - grounded
    #: in `engine.execution.ranking.pick_superlative`, never a model
    #: guess). When set, `node_respond` uses it directly instead of
    #: running model synthesis - the same "a real computed fact beats a
    #: model's free text" discipline `compare.py`'s own superlative
    #: handling already applies, now shared instead of living in one
    #: capability only.
    deterministic_reply: str | None
    #: True when the shopper explicitly asked for "all"/"every" but this
    #: turn's own retrieval was itself already capped by the platform's
    #: real total-count - see `node_discovery`'s own comment.
    show_all_truncated: bool

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

    # Signed-in-shopper-only durable memory, mirroring chat.py's own
    # persist exactly (same db.memory_facts.upsert call, same best-effort
    # try/except so a hiccup here never fails the turn). Live-reproduced
    # gap this closes: `understand()` reuses V1's own `reason()` call, which
    # already extracts `durable_facts` every turn - V2 read memory facts
    # (chat_v2.py's own fetch, passed into ranking) but never WROTE any,
    # so a shopper's "I really prefer lightweight gear" was acknowledged in
    # the reply but never reached storage - the personalization ranking
    # itself (mission_ranking.rank_and_filter) was correct and reachable
    # only by seeding the database directly, never by a real conversation.
    shopper_id = state.get("shopper_id")
    if shopper_id and result.durable_facts:
        try:
            for fact in result.durable_facts:
                await memory_facts_store.upsert(
                    state["connection_id"],
                    shopper_id,
                    key=fact["key"],
                    value=fact["value"],
                    source_session_id=state["session_id"],
                )
        except Exception:  # noqa: BLE001
            logger.exception("could not persist shopper_v2 durable facts; continuing")

    return {
        "delta": result.delta,
        "hinted_action_type": result.hinted_action_type,
        "understanding_hinted_product_id": result.hinted_product_id,
        "hinted_order_id": result.hinted_order_id,
        "message_intent": result.message_intent,
        "conversational_reply": result.conversational_reply,
        "trace": trace,
    }


def _fulfillment_pending_dict(new_state: ShoppingStateV2) -> dict | None:
    """The one currently-pending `fulfillment.py` question, if any, in the
    shape a future frontend needs to render real option buttons
    (`selected_variant_id` on the next `ChatRequestV2` is how a tap answers
    it) - `None` when nothing is pending. Purely a read of already-real,
    already-verified state; invents nothing."""
    item = next(
        (i for i in new_state.fulfillment_queue if i.status == "needs_variant"), None
    )
    if item is None:
        return None
    return {
        "product_id": item.product_id,
        "product_title": item.title,
        "dimension_label": item.dimension_label,
        "options": item.options,
    }


async def node_apply_delta(state: ShopperTurnState) -> dict:
    new_state = await apply_delta(state["current_state"], state["delta"])
    trace = state["trace"]
    trace.state_version = new_state.version
    return {"new_state": new_state, "trace": trace}


async def node_fulfillment_continue(state: ShopperTurnState) -> dict:
    """Answers a pending `fulfillment.py` question, if this turn's message
    resolves it or is a plausible attempt to. When `handled=False` (no
    pending question, or - see `continue_fulfillment`'s own docstring -
    this message plainly abandoned the pending one), no `result` is set
    and the graph falls through to ordinary routing for this SAME message,
    via `edge_after_fulfillment_continue` below - never left stuck
    answering a question nobody is trying to answer anymore."""
    trace = state["trace"]
    new_state = state["new_state"]
    reply_text, added_ids, handled = await fulfillment.continue_fulfillment(
        adapter=state["adapter"], decision=state["decision"], gate=state["gate"],
        execution=state["execution"], connection_id=state["connection_id"],
        session_id=state["session_id"], cart_id=state.get("cart_id"),
        message=state["message"], state=new_state,
        selected_variant_id=state.get("selected_variant_id"),
    )
    if not handled:
        return {"trace": trace, "new_state": new_state}
    for product_id in added_ids:
        await _record_event(
            connection_id=state["connection_id"], session_id=state["session_id"],
            shopper_id=state.get("shopper_id"),
            event_type=shopper_events.EventType.ADD_TO_CART,
            product_id=product_id,
            detail={"source": "multi_item_add"},
        )
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=new_state, evidence=evidence, used_model=False,
        trace={
            **trace.as_dict(),
            "cart_added_product_ids": added_ids,
            "fulfillment_pending": _fulfillment_pending_dict(new_state),
        },
    )
    return {"trace": trace, "new_state": new_state, "result": result}


def edge_after_fulfillment_continue(state: ShopperTurnState) -> str:
    if state.get("result") is not None:
        return END
    return _edge_after_fulfillment_check(state)


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
    # Highest precedence, above mission/tradeoff/ordinary routing: a
    # pending multi-product fulfillment question (`fulfillment.py`) is a
    # direct question THIS assistant just asked. This ALWAYS visits
    # `fulfillment_continue` when one is pending - deliberately NOT
    # pre-filtered by a second, separate "does this look like an answer"
    # guess here (a prior version of this function tried that with a
    # phrase/comma heuristic and it caused two different live-reproduced
    # bugs: a genuine reply routed away without ever clearing the stale
    # question, permanently trapping every later message; and a genuine
    # button tap's own synthesized sentence, which happens to contain a
    # comma, was misread as a topic change). `fulfillment.py::
    # continue_fulfillment` is the ONE place that decides continue-vs-
    # abandon, against the pending item's own REAL options
    # (`looks_like_answer_attempt`) - abandoning there always clears the
    # stale state AND falls through to ordinary routing for this same
    # message (`edge_after_fulfillment_continue` below), so a single
    # source of truth can never leave this gate and that decision
    # disagreeing with each other.
    if (
        friction is None
        and new_state.fulfillment_queue
        and any(i.status == "needs_variant" for i in new_state.fulfillment_queue)
        and state.get("decision") is not None
        and state.get("gate") is not None
        and state.get("execution") is not None
    ):
        return "fulfillment_continue"
    return _edge_after_fulfillment_check(state)


def _edge_after_fulfillment_check(state: ShopperTurnState) -> str:
    """The mission/tradeoff/route_capability decision, unchanged from
    before the fulfillment-pending gate existed - factored out so
    `edge_after_fulfillment_continue` can fall through to the EXACT same
    ordinary routing when `fulfillment.py` itself decides a pending
    question was abandoned (see that function's own docstring), not a
    second, potentially-diverging copy of this logic."""
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
        # Deterministic reference backstop - see capability.py's own
        # docstring on `SOFT_CAPABILITIES`/`looks_like_reference`. This is
        # a routing-PRECEDENCE fix, not a phrase fix: `route()`'s single
        # primary-model call can land on ANY of `SOFT_CAPABILITIES` with no
        # independent confirmation behind it - a genuine reference
        # ("what was the cheapest shoe you showed me earlier", "the first
        # one", "both", "the cheaper one") must override every one of
        # those equally, not only the two the model happens to default to
        # (DISCOVER/CONVERSATIONAL) - live-reproduced landing on
        # COMPATIBILITY instead. Only ever overrides a SOFT guess, and only
        # when there is genuinely at least one real, already-tracked
        # product to resolve against - never invents a reference target
        # out of nothing. `run_compare_turn` itself refuses to answer
        # anything it cannot resolve from real structure (ordinal/group
        # selectors, the historical-group matcher, the comparative-pair
        # resolver) - this only decides whether that logic gets a turn to
        # try, never what it may conclude.
        if (
            capability in SOFT_CAPABILITIES
            and looks_like_reference(state["message"])
            and len(recent_relevant_product_ids(state["new_state"], minimum=1)) >= 1
        ):
            capability = Capability.COMPARE
        # Deterministic cart-intelligence backstop - see capability.py's
        # own docstring on the live-reproduced gap this closes ("My cart
        # is over 10000..." fell through to a literal catalog search for
        # those words instead of analyzing the real cart). Same precedence
        # fix as above - overrides any SOFT guess, and only when a cart
        # genuinely exists for this turn - never invents cart analysis out
        # of nothing.
        elif (
            capability in SOFT_CAPABILITIES
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
    if hinted_product_ids and len(hinted_product_ids) >= 2 and state.get("cart_operation") == "add":
        # Shopper item 3: a genuine plural add ("add both") starts a
        # conversational fulfillment queue - one real product/variant
        # question at a time, each answer added to the real cart directly
        # (never a proposal sitting pending on a separate confirm) - see
        # `fulfillment.py`'s own module docstring for the full argument.
        reply_text, added_ids = await fulfillment.start_fulfillment(
            adapter=state["adapter"], decision=state["decision"], gate=state["gate"],
            execution=state["execution"], connection_id=state["connection_id"],
            session_id=state["session_id"], cart_id=state.get("cart_id"),
            message=state["message"], state=state["new_state"],
            product_ids=hinted_product_ids, quantity=state.get("hinted_quantity"),
        )
        for product_id in added_ids:
            await _record_event(
                connection_id=state["connection_id"], session_id=state["session_id"],
                shopper_id=state.get("shopper_id"),
                event_type=shopper_events.EventType.ADD_TO_CART,
                product_id=product_id,
                detail={"source": "multi_item_add"},
            )
        evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
        trace.response_used_model = False
        trace.finish()
        result = TurnResultV2(
            reply=reply_text, state=state["new_state"], evidence=evidence,
            used_model=False,
            trace={
                **trace.as_dict(),
                "cart_added_product_ids": added_ids,
                "fulfillment_pending": _fulfillment_pending_dict(state["new_state"]),
            },
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

async def _try_superlative_from_existing_group(
    state: ShopperTurnState, query_intent,
) -> dict | None:
    """Answers a bare superlative question ("what is the most expensive
    shoe?", no historical wording) from a REAL, already-established, LARGER
    group already in view this session, instead of a fresh, possibly much
    narrower retrieval - see `node_discovery`'s own comment for the exact
    live-reproduced gap this closes. Returns a complete node-result dict
    when it can genuinely answer this way, `None` when it can't (no
    established group, too small a group, or the established group isn't
    actually related to what THIS turn asked about) - the caller then
    falls through to the ordinary fresh-retrieval path unchanged."""
    new_state = state["new_state"]
    ids = recent_relevant_product_ids(new_state, minimum=2, cap=20)
    if len(ids) < 2:
        return None

    adapter = state["adapter"]
    products: list[Product] = []
    for pid in ids:
        try:
            products.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001 - skip what can't be fetched, never guess
            logger.warning(
                "shopper_v2 discovery: superlative-reuse get_product failed for %r",
                pid, exc_info=True,
            )
    if len(products) < 2:
        return None

    # Never blindly reuse an unrelated leftover group - the established
    # group must actually share a real word with THIS turn's own core
    # query (the same word-overlap check `compare.py`'s historical-group
    # matching already uses), or this returns None and the caller runs a
    # real, fresh search instead.
    core = query_intent.core_query or state["message"]
    score, _ = best_matches_with_score(products, core)
    if score <= 0:
        return None

    pick = ranking.pick_superlative(products, query_intent.sort_attribute, query_intent.superlative)
    if pick is None:
        return None

    trace = state["trace"]
    evidence = already_verified_evidence(
        [pick], {pick.product_id: ["reused_established_group_superlative"]}
    )
    trace.response_used_model = False
    return {
        "trace": trace,
        "evidence": evidence,
        "products": [pick],
        "deterministic_reply": _superlative_reply(pick, query_intent),
        "show_all_truncated": False,
    }


async def node_discovery(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    delta = state["delta"]
    new_state = state["new_state"]
    # SearchIntent, genuinely retrieval-driving: a deterministic signal from
    # THIS turn's real StateDelta (never a guess at word content) that this
    # turn stated a new budget with no new goal of its own.
    constraint_only_turn = delta.budget_amount is not None and not delta.goal

    # Root-cause consolidation (2026-09): cardinality, sorting, and
    # superlatives are now ALL decided from the SAME typed `QueryIntent`
    # (`reference_resolution.parse_query_intent`) in this ONE place -
    # never a separate word-list per capability. Computed BEFORE
    # retrieval (not just after) so `core_query` - the message with
    # recognized count/sort/superlative PHRASES stripped - can replace
    # the raw message as the retrieval/relevance-scoring text whenever
    # the model itself supplied no rewritten query. Live-reproduced,
    # generalized gap this closes: "ranked by price high to low" left
    # unstripped let the bare word "high" spuriously substring-match
    # "Highridge..." during relevance scoring, starving every other
    # genuine match - see `QueryIntent.core_query`'s own docstring.
    query_intent = parse_query_intent(state["message"])

    # Superlative-scope fix (live-reproduced): "what is the most expensive
    # shoe?" with no prior reference wording used to always trigger a
    # BRAND-NEW, narrowly-scoped retrieval for that one turn, and compute
    # the superlative only over whatever subset the adapter's own keyword
    # search happened to return this time - which is not guaranteed to
    # include the actual highest-priced item, and visibly didn't in the
    # reported case. When a larger, genuinely related group is ALREADY
    # established this session (`recent_relevant_product_ids` - the same
    # ledger historical references already use), the superlative is
    # answered from that real, already-verified, larger group instead of
    # re-searching from scratch.
    if query_intent.superlative and query_intent.sort_attribute in ranking.KNOWN_ATTRIBUTES:
        reused = await _try_superlative_from_existing_group(state, query_intent)
        if reused is not None:
            return reused

    delta_query = delta.search_query or query_intent.core_query

    evidence, products, _intent = await run_discovery_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], session_id=state["session_id"],
        connection_id=state["connection_id"], message=state["message"],
        delta_query=delta_query, constraint_only_turn=constraint_only_turn,
        shopping_state=new_state.as_prompt_dict(), memory_facts=state.get("memory_facts"),
        trace=trace,
    )
    # Every downstream consumer (the reply text in `respond.py`, the
    # product cards in `finalize`, and `active_products`/
    # `recent_product_groups` state) reads the SAME resulting `products`
    # list, so a display/order decision made once here can never be
    # contradicted by a different one computed somewhere else downstream.
    evidence, products, deterministic_reply = _apply_query_intent(evidence, products, query_intent)
    # Real-volume honesty (CLAUDE.md's own standing condition): a plain,
    # uncounted request ("show me shoes") or an explicit "show me all
    # shoes" must never silently claim to be the whole catalog when this
    # turn's own retrieval was itself already capped
    # (`trace.candidate_set_truncated`, set by `pipeline.py` from the
    # adapter's real `total_available` count, e.g. a 40k-SKU catalog with
    # only a page fetched this turn) - `node_respond` appends an honest
    # disclosure sentence rather than letting either case silently imply
    # completeness when it wasn't.
    #
    # Product decision (2026-09, corrected after live feedback): a plain
    # "show me shoes" with NO stated count used to default to a fixed
    # display cap of 4 - a real fix for a real bug (reply text disagreeing
    # with the card count) at the time, but the wrong default for what
    # this product actually wants: an uncounted request should show
    # everything genuinely found this turn, exactly like an explicit
    # "all" does, not a capped preview. `_apply_query_intent` no longer
    # applies any default cap; the disclosure above now covers BOTH cases
    # identically, since both can equally be a partial view of a larger
    # real catalog.
    show_all_truncated = bool(
        (query_intent.show_all or query_intent.requested_count is None)
        and trace.candidate_set_truncated
    )
    return {
        "trace": trace, "evidence": evidence, "products": products,
        "deterministic_reply": deterministic_reply,
        "show_all_truncated": show_all_truncated,
    }


def _superlative_reply(product: Product, query_intent) -> str:
    which = "cheapest" if query_intent.superlative == "min" else "most expensive"
    if query_intent.sort_attribute == "rating":
        which = "best rated" if query_intent.superlative == "max" else "lowest rated"
        return f"The {which} one is {product.title}, rated {product.rating}."
    return f"The {which} one is {product.title}, at {product.price}."


def _apply_query_intent(
    evidence: TurnEvidence, products: list[Product], query_intent,
) -> tuple[TurnEvidence, list[Product], str | None]:
    """Reorders (sort/superlative) and caps (count/all) the real,
    relevance-verified products for this turn, from ONE typed intent -
    never a second, local count/sort decision anywhere else. Returns
    `(evidence, products, deterministic_reply)` - CRITICALLY, `evidence`
    is a NEW `TurnEvidence` whose own `.products` is reordered to match
    the same final order as the returned `products` list.

    Root-cause fix, second half: reordering `products` alone (a prior
    version of this function did only that) is not enough - `respond.py`'s
    reply-text ordering and `node_finalize`'s persisted
    `active_products`/`recent_product_groups` order BOTH iterate
    `evidence.products`, not this function's own `products` return value.
    Live-reproduced: "three shoes ranked high to low" showed the cards in
    the correct sorted order but named them in a DIFFERENT order in the
    reply text, and a later "add the first two" resolved against the
    WRONG first product - because the evidence order and the card order
    had silently diverged, the exact class of "two representations of the
    same thing, not kept in sync" bug this whole consolidation exists to
    close. Returning one, single reordered `TurnEvidence` from this one
    function is what makes every downstream consumer agree by
    construction, not by each one separately remembering to reorder
    itself.

    The reply is set (bypassing model synthesis entirely - see
    `node_respond`) only for a genuine superlative request that a real
    product actually answers; `None` otherwise, exactly as before."""
    verified_ids = [e.product_id for e in evidence.products if e.relevance_verified]
    by_id = {p.product_id: p for p in products}
    ordered_verified = [by_id[pid] for pid in verified_ids if pid in by_id]

    deterministic_reply: str | None = None
    if query_intent.superlative and query_intent.sort_attribute in ranking.KNOWN_ATTRIBUTES:
        pick = ranking.pick_superlative(
            ordered_verified, query_intent.sort_attribute, query_intent.superlative
        )
        if pick is not None:
            deterministic_reply = _superlative_reply(pick, query_intent)
            ordered_verified = [pick]
    elif query_intent.sort_attribute in ranking.KNOWN_ATTRIBUTES and query_intent.sort_direction:
        ordered_verified = ranking.sort_products(
            ordered_verified, query_intent.sort_attribute, query_intent.sort_direction
        )

    if not deterministic_reply:
        # No default cap: an explicit "all", and a plain uncounted
        # request alike, show every real, relevance-verified product this
        # turn found - never a fixed preview size. Only an EXPLICIT count
        # ("show me two running shoes") caps the display, to exactly that
        # number.
        if query_intent.requested_count is not None:
            cap = query_intent.requested_count
        else:
            cap = len(ordered_verified)
        ordered_verified = ordered_verified[:cap] if cap > 0 else []

    keep_ids = {p.product_id for p in ordered_verified}
    all_verified_ids = set(verified_ids)
    # Preserve `ordered_verified`'s own order for the verified ones (so
    # sort/superlative actually shows up in the cards), followed by any
    # NEVER-verified products `respond.py`/`finalize` already ignore
    # anyway - deliberately NOT the verified-but-capped-away ones (that
    # would silently defeat the whole cap by re-adding them here).
    final = list(ordered_verified) + [
        p for p in products
        if p.product_id not in keep_ids and p.product_id not in all_verified_ids
    ]
    final_ids = [p.product_id for p in final]
    evidence_by_id = {e.product_id: e for e in evidence.products}
    reordered_evidence_products = [
        evidence_by_id[pid] for pid in final_ids if pid in evidence_by_id
    ] + [e for e in evidence.products if e.product_id not in set(final_ids)]
    reordered_evidence = evidence.model_copy(update={"products": reordered_evidence_products})
    return reordered_evidence, final, deterministic_reply


#: Appended, never prepended or blended into the model/deterministic
#: reply's own sentence - keeps the disclosure honest and separable from
#: whatever else the reply says, and identical regardless of which reply
#: path produced the rest of the text.
_SHOW_ALL_TRUNCATED_NOTE = (
    " (Showing everything this search found - there may be more in the "
    "full catalog than one search page returns.)"
)


async def node_respond(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    evidence = state["evidence"]
    products = state["products"]
    new_state = state["new_state"]
    show_all_truncated = state.get("show_all_truncated", False)
    deterministic_reply = state.get("deterministic_reply")
    if deterministic_reply:
        trace.response_used_model = False
        return {"trace": trace, "reply_text": deterministic_reply, "used_model": False}
    reply_text, used_model = await respond_(
        reasoning=state["reasoning"], message=state["message"], evidence=evidence,
        products=products, state=new_state, memory_facts=state.get("memory_facts"),
    )
    if show_all_truncated:
        reply_text = reply_text + _SHOW_ALL_TRUNCATED_NOTE
    trace.response_used_model = used_model
    return {"trace": trace, "reply_text": reply_text, "used_model": used_model}


async def node_finalize(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    evidence = state["evidence"]
    products = state["products"]
    new_state = state["new_state"]
    session_id = state["session_id"]
    connection_id = state["connection_id"]

    # Intersected with `products` (already cardinality-capped in
    # `node_discovery`) - never the raw, uncapped evidence list - so the
    # state persisted as "what was just shown" (`active_products`/
    # `recent_product_groups`, read back by every later reference/compare
    # turn) can never name more products than actually rendered as cards
    # this turn.
    available_ids = {p.product_id for p in products}
    verified_ids = [
        e.product_id for e in evidence.products
        if e.relevance_verified and e.product_id in available_ids
    ]
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
    g.add_node("fulfillment_continue", node_fulfillment_continue)
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
        {
            "fulfillment_continue": "fulfillment_continue",
            "mission": "mission",
            "tradeoff": "tradeoff",
            "route_capability": "route_capability",
        },
    )
    g.add_conditional_edges(
        "fulfillment_continue", edge_after_fulfillment_continue,
        {END: END, "mission": "mission", "tradeoff": "tradeoff", "route_capability": "route_capability"},
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
    selected_variant_id: str | None = None,
) -> TurnResultV2:
    """Run one V2 turn through the compiled graph. Same signature and
    return type as the pre-graph `orchestrator.run_turn` - `catalog_sample`
    is accepted, unused, for signature compatibility (it was already
    unused in the pre-graph implementation). `selected_variant_id` is a
    tapped variant-option button, when the frontend sends one - see
    `fulfillment.py`'s own docstring."""
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
        "selected_variant_id": selected_variant_id,
    }
    final_state = await _COMPILED_GRAPH.ainvoke(initial)
    return final_state["result"]
