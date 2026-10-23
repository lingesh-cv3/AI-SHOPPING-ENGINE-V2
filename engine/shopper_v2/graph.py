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
import re
import uuid
from dataclasses import dataclass, field, replace as _dataclass_replace
from typing import Any, TypedDict

from langgraph.graph import END, StateGraph

from shared.models import Product

from fastapi import HTTPException

from engine.api.chat import accept_cart_proposal_core
from engine.db import cart_proposals as cart_proposals_store
from engine.db import memory_facts as memory_facts_store
from engine.db import shopping_state as shopping_state_store
from engine.execution import events as shopper_events
from engine.reasoning.service import ReasoningService

from .capability import (
    SOFT_CAPABILITIES,
    Capability,
    looks_like_cart_intelligence_request,
    looks_like_collective_comparison,
    looks_like_comparison_request,
    looks_like_preference_status_request,
    looks_like_bare_recommendation_imperative,
    looks_like_explicit_cart_command,
    looks_like_question,
    looks_like_reference,
    looks_like_resume_request,
    looks_like_state_change_shape,
    route,
)
from .cart_proposal import run_cart_change_proposal_turn
from .checkout import run_checkout_turn
from .add_command import is_explicit_add_command, names_an_add as _names_an_add, reference_is_ambiguous
from .compare import run_compare_turn
from .store_discovery import looks_like_store_discovery, run_store_discovery_turn
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
from . import guided_mission, named_product
from .pipeline import _buyable
from .missions import run_mission_turn
from .pipeline import run_discovery_turn
from .reference_resolution import (
    _distinctive_title_words,
    looks_like_group_or_ordinal_reference,
    parse_query_intent,
    resolve_group_selector,
)
from engine.execution import ranking
from engine.execution.relevance import best_matches_with_score
from .respond import _claims_a_cart_action, grounded_reply_or_fallback
from .respond import respond as respond_
from .state import (
    LastRendered,
    ShoppingStateV2,
    Topic,
    apply_delta,
    current_view_product_ids,
    load,
    recent_relevant_product_ids,
)
from .topic_transition import TransitionResult, apply_transition, decide_transition
from .tracing import TurnTrace
from .tradeoff import run_tradeoff_turn
from .conversation_context import ConversationContext, build_context
from . import interpretation
from .interpretation import interpret_turn
from .verification import VerifiedInterpretation, verify
from . import vocabulary
from . import answer
from .answer import run_alternatives, run_explanation, run_recommendation, run_relationship_or_compound
from .retrieval import retrieve
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


async def _fetch_products(adapter, product_ids: set[str] | list[str]) -> list:
    """Real, freshly-fetched products for a set of ids - never invented,
    never cached across turns. A per-id fetch failure is skipped, not
    raised - the same honest-degrade discipline `compare.py`'s own
    historical-group fetch already uses."""
    fetched = []
    for pid in product_ids:
        try:
            fetched.append(await adapter.get_product(pid))
        except Exception:  # noqa: BLE001
            logger.warning("shopper_v2: get_product failed for %r during pre-grounding", pid, exc_info=True)
    return fetched


def _reply_for_verified_reference(verified: VerifiedInterpretation, products: list) -> str:
    """Rev. 2 architecture (2026-09), Phase 4: an honest, grounded reply
    built directly from a VERIFIED reference - never a model call, never a
    guess. Mirrors the plain, factual tone `compare.py`'s own deterministic
    superlative/named replies already use."""
    ref = verified.resolved_reference
    if ref is None or not products:
        return "I couldn't find that among what I can see right now."
    if len(products) == 1:
        p = products[0]
        price_text = f", at {p.price}" if p.price is not None else ""
        return f"That's {p.title}{price_text}."
    names = ", ".join(p.title for p in products)
    return f"Here's what you asked for: {names}."


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
    #: The structured search term of a DEAD_SEARCH hand-off (see
    #: `ChatRequestV2.friction_query`); None for everything else.
    friction_query: str | None
    #: Set by `node_guided_turn` when it could not apply a guided-mission
    #: command to a real stage, so routing continues as for any other turn.
    guided_declined: bool
    #: Set by `node_apply_delta` when the whole message is a typed confirmation
    #: ("yes", "approve it", "add it"): `{"kind": "confirm"|"add_pointer",
    #: "pending": [this session's pending proposals on this cart]}`.
    typed_confirm: dict
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
    #: Rev. 2 architecture (2026-09) - `conversation_context.py`'s own
    #: docstring. Built every turn (Phase 1 of the migration); not yet
    #: consulted by any decision path - purely additive until later
    #: migration phases wire capability nodes to read from it.
    conversation_context: Any
    delta: Any  # StateDelta
    new_state: ShoppingStateV2
    #: Production architecture (2026-09), Phase 3: the most recent real,
    #: PENDING CartProposal for this turn's cart, if any - fetched once in
    #: `node_apply_delta`, consumed by `edge_after_apply_delta`/
    #: `node_cart_proposal_continue`. `None` when there is none, or no
    #: `cart_id` this turn.
    pending_cart_proposal: dict | None
    hinted_action_type: str | None
    hinted_order_id: str | None
    understanding_hinted_product_id: str | None
    message_intent: str | None
    conversational_reply: str | None

    capability: Capability
    #: True only when `node_route_capability`'s own CONFIRMED reference
    #: backstop (`looks_like_reference`, a real product to resolve
    #: against) promoted capability to COMPARE - never true when COMPARE
    #: came from the primary model's own unconfirmed hint alone. See
    #: `edge_after_route_capability`'s own comment on the live-reproduced
    #: false positive this distinction closes.
    capability_confirmed_reference: bool
    #: Rev. 2 architecture (2026-09) - `verification.py::VerifiedInterpretation`
    #: from `node_route_capability`'s own interpretation attempt, when one
    #: ran. Carries `question_type` and the resolved primary/secondary
    #: entities `node_answer_question` needs - never re-derived by that
    #: node itself when this is already present.
    verified_interpretation: Any
    #: Production architecture (2026-09), Phase 1: True when THIS turn's
    #: real interpretation call was attempted (regardless of outcome) -
    #: distinguishes "declined to try" (a downstream node's own, narrower
    #: gate may still legitimately try) from "tried and failed" (no
    #: downstream node may retry - see the "no cascading LLM fallback"
    #: rule, production architecture review §3).
    interpretation_attempted: bool
    #: The real, already-fetched products `node_route_capability`'s own
    #: interpretation attempt fetched this turn (if any) - reused by every
    #: downstream node that needs the same products `verified_interpretation`
    #: was resolved against, instead of re-fetching them.
    interpretation_products: list
    #: Production architecture (2026-09), Phase 1: True when `node_
    #: reference_resolution` already made its own real interpretation
    #: attempt for an explicit cart command this turn (whatever the
    #: outcome) - `node_cart_action`'s own second-safety-net attempt must
    #: not retry the identical question (the "no cascading LLM fallback"
    #: rule).
    cart_interpretation_attempted: bool
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

    #: The Focus Stack transition decided for this turn (see
    #: topic_transition.py) - computed once, right after `apply_delta`,
    #: and read by every later node/edge that needs to know which topic is
    #: authoritative for this turn rather than recomputing it.
    topic_decision: Any
    #: Set only when `topic_decision.action == "push"` - the id the new
    #: Topic will use once `node_finalize`/`node_mission` actually create
    #: it (generated early so both possible creators agree on the same id).
    new_topic_id: str | None

    # Terminal output - set by whichever node ends the turn.
    result: TurnResultV2


# --------------------------------------------------------------------------
# Node 1-2: load context, understand
# --------------------------------------------------------------------------

async def node_load_context(state: ShopperTurnState) -> dict:
    current_state = await load(state["session_id"], state["connection_id"])
    turn_context = await build_turn_context(state["session_id"], state["connection_id"])
    trace = TurnTrace(session_id=state["session_id"], connection_id=state["connection_id"])
    # Rev. 2 architecture (2026-09), Phase 1: build the one authoritative
    # ConversationContext every turn. Purely additive here - nothing reads
    # `conversation_context` yet, so this cannot change any existing
    # behavior; it exists so later migration phases can wire capability
    # nodes to it one at a time (see the approved migration plan) instead
    # of a single big-bang cutover.
    conversation_context = await build_context(
        state=current_state,
        adapter=state["adapter"],
        cart_id=state.get("cart_id"),
        history=turn_context.history,
        memory_facts=state.get("memory_facts"),
    )
    return {
        "current_state": current_state,
        "history": turn_context.history,
        "trace": trace,
        "conversation_context": conversation_context,
    }


async def node_understand(state: ShopperTurnState) -> dict:
    """Production architecture (2026-09), Phase 1 reconciliation: THE
    single authoritative interpretation call for this turn. Gathers the
    same real, already-verified products `node_route_capability`'s own
    (now-retired) conditional interpretation attempt used to fetch, calls
    `understand()` (which now calls `interpretation.interpret_turn()`
    internally - see that module's own docstring), and immediately
    resolves the raw claim via `verify()` - real `ConversationContext`/
    products are already available at this point in the turn (right after
    `node_load_context`), so there is no reason to defer verification to a
    later node the way the old, two-call design had to. Every downstream
    node (topic_transition, route_capability, reference_resolution,
    compare, compatibility/complementary, cart_action, answer_question)
    consumes `state["verified_interpretation"]` from here on - none of
    them attempts its own, independent interpretation call for the SAME
    turn's meaning again."""
    trace = state["trace"]
    context: ConversationContext | None = state.get("conversation_context")

    active_topic_products: list = []
    last_rendered_products: list = []
    all_fetched_products: list = []
    active_topic_label_override: str | None = None
    if context is not None:
        ids_to_fetch: set[str] = set()
        if context.active_topic:
            ids_to_fetch |= set(context.active_topic.product_ids)
        if context.last_rendered:
            ids_to_fetch |= set(context.last_rendered.product_ids)
        for t in context.paused_topics:
            ids_to_fetch |= set(t.product_ids)
        # Natural-language cart-operation fix (2026-09): a real cart line is
        # a valid reference scope now (`reference_scope="cart"` -
        # `verification.py::_candidates_for_scope`'s own docstring) - its
        # product ids must be in the fetched pool `verify()` builds
        # `products_by_id` from, or a genuinely resolved cart reference
        # would still come back empty.
        if context.cart_snapshot:
            ids_to_fetch |= {line.product_id for line in context.cart_snapshot.lines}
        legacy_active_ids: set[str] = set()
        if not ids_to_fetch:
            # Legacy-bridge fallback (same discipline `conversation_
            # context.py::build_context` already uses for paused topics):
            # a session whose `active_products` was set directly, never
            # through a real Focus Stack push, still has a real, current
            # product set worth reasoning about.
            legacy_active_ids = set(recent_relevant_product_ids(state["current_state"], minimum=1))
            ids_to_fetch |= legacy_active_ids
        if ids_to_fetch:
            all_fetched_products = await _fetch_products(state["adapter"], ids_to_fetch)
            legacy_active_products = [p for p in all_fetched_products if p.product_id in legacy_active_ids]
            active_topic_products = [
                p for p in all_fetched_products
                if (context.active_topic and p.product_id in context.active_topic.product_ids)
                or p.product_id in legacy_active_ids
            ]
            last_rendered_products = [
                p for p in all_fetched_products
                if context.last_rendered and p.product_id in context.last_rendered.product_ids
            ]
            active_topic_label_override = (
                ", ".join(p.title for p in legacy_active_products[:3])
                if legacy_active_products and not context.active_topic else None
            )

    # The merchant's REAL category taxonomy, so the interpreter can map a
    # shopper's non-category word ("beans") onto categories that exist -
    # never asked to invent one (see `vocabulary.py`).
    try:
        _vocab = await vocabulary.get_vocabulary(state["adapter"], state["connection_id"])
    except Exception:  # noqa: BLE001 - no taxonomy is a degrade, not a failure
        _vocab = None
    store_categories = list(_vocab.categories) if _vocab is not None else None
    spelling_hint = None
    if _vocab is not None:
        _fixed, _corrections = vocabulary.spell_only(state["message"], _vocab)
        if _corrections:
            spelling_hint = _fixed

    result = await understand(
        reasoning=state["reasoning"],
        message=state["message"],
        history=state["history"],
        shopping_state=state["current_state"].as_prompt_dict(),
        memory_facts=state.get("memory_facts"),
        context=context,
        active_topic_products=active_topic_products,
        last_rendered_products=last_rendered_products,
        all_fetched_products=all_fetched_products,
        active_topic_label_override=active_topic_label_override,
        store_categories=store_categories,
        spelling_hint=spelling_hint,
    )
    trace.understanding_used_model = result.used_model
    trace.understanding_model_name = result.model_name
    trace.interpretation_used_model = result.used_model

    claim = result.interpretation_claim
    verified_interpretation = None
    if claim is not None and context is not None:
        verified_interpretation = verify(claim, context, all_fetched_products, state["message"])
        # Bounded escalation (Gap D, closed generally rather than wired to
        # one capability - production architecture review §3): one
        # narrowed retry when the first pass found real candidates but
        # couldn't pick among them unambiguously - never when nothing
        # resolved at all.
        if (
            verified_interpretation.resolved_reference is not None
            and verified_interpretation.resolved_reference.ambiguous
            and verified_interpretation.resolved_reference.candidates_considered
            and interpretation.REASONING_BUDGET >= 2
        ):
            narrowed = verified_interpretation.resolved_reference.candidates_considered
            escalation_context = context.model_copy(
                update={
                    "last_rendered": LastRendered(
                        product_ids=[p.product_id for p in narrowed],
                        source_capability="escalation",
                    ),
                },
            )
            escalated_claim = await interpretation.escalate_ambiguous_reference(
                reasoning=state["reasoning"], message=state["message"],
                context=escalation_context, candidates=narrowed,
            )
            if escalated_claim is not None:
                escalated_verified = verify(escalated_claim, escalation_context, narrowed, state["message"])
                if (
                    escalated_verified.resolved_reference is not None
                    and escalated_verified.resolved_reference.product_ids
                    and not escalated_verified.resolved_reference.ambiguous
                ):
                    verified_interpretation = escalated_verified

    # Signed-in-shopper-only durable memory, mirroring chat.py's own
    # persist exactly (same db.memory_facts.upsert call, same best-effort
    # try/except so a hiccup here never fails the turn).
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
        "verified_interpretation": verified_interpretation,
        "interpretation_attempted": context is not None,
        "interpretation_products": all_fetched_products,
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


#: Production architecture (2026-09), Phase 3: the repair shapes a pending
#: CartProposal actually needs a reply for. Checked here (cheaply, before
#: the DB lookup below even runs) so an ordinary turn with no bearing on a
#: pending proposal never pays for the lookup at all.
_CART_PROPOSAL_REPAIRS = ("confirmation", "cancellation", "correction")


#: A typed confirmation is only ever the WHOLE message, drawn from a small closed
#: class - never a phrase buried in a longer sentence. "add it"/"add that" are the
#: pointer-shaped ones: they confirm a pending proposal only when nothing else is on
#: screen that they could mean (see `edge_after_apply_delta`).
_CONFIRM_PHRASES = frozenset({
    "yes", "yep", "yeah", "yup", "sure", "ok", "okay", "alright", "yes please", "please do",
    "go ahead", "yes go ahead", "do it", "yes do it", "go for it", "approve", "approve it",
    "approve that", "approve this", "yes approve it", "confirm", "confirm it", "confirm that",
    "yes confirm", "confirmed", "approved", "sounds good", "that works",
})
_ADD_POINTER_PHRASES = frozenset({
    "add it", "add that", "add this", "add that one", "add this one", "yes add it",
    "add it please", "please add it", "yes add that",
})


def _confirmation_kind(message: str) -> str | None:
    norm = " ".join(re.sub(r"[^a-z' ]", " ", (message or "").lower()).split())
    if not norm or len(norm.split()) > 4:
        return None
    if norm in _ADD_POINTER_PHRASES:
        return "add_pointer"
    if norm in _CONFIRM_PHRASES:
        return "confirm"
    return None


async def node_apply_delta(state: ShopperTurnState) -> dict:
    new_state = await apply_delta(state["current_state"], state["delta"])
    trace = state["trace"]
    trace.state_version = new_state.version
    result: dict[str, Any] = {"new_state": new_state, "trace": trace}

    # A typed confirmation needs the proposals actually waiting on THIS session's
    # cart (scoped by connection, cart and session - the same lookup the accept
    # route uses), read here once so routing can decide deterministically.
    _kind = _confirmation_kind(state["message"])
    if _kind and state.get("cart_id"):
        try:
            _waiting = await cart_proposals_store.pending_for_cart(
                state["connection_id"], state["cart_id"], session_id=state["session_id"],
            )
        except Exception:  # noqa: BLE001 - never fail the turn over this lookup
            logger.warning("typed-confirmation pending lookup failed", exc_info=True)
            _waiting = []
        result["typed_confirm"] = {"kind": _kind, "pending": _waiting}

    # Production architecture (2026-09), Phase 3: one coherent conversational
    # PendingAction authority. `fulfillment_queue` (in-memory, on `new_state`
    # already) and `pending_question` (read directly inside
    # `node_answer_question`) each still own their own real storage and
    # matching logic unchanged - this only adds the missing third case,
    # CartProposal, to the SAME precedence check `edge_after_apply_delta`
    # already makes for fulfillment_queue. A real DB lookup, not a second
    # persisted state - the CartProposal row itself remains the single
    # source of truth (see `accept_cart_proposal_core`'s own docstring).
    verified: VerifiedInterpretation | None = state.get("verified_interpretation")
    cart_id = state.get("cart_id")
    repair = verified.conversational_repair if verified is not None else "none"
    if cart_id and repair in _CART_PROPOSAL_REPAIRS:
        try:
            pending = await cart_proposals_store.pending_for_cart(
                state["connection_id"], cart_id, session_id=state["session_id"],
            )
        except Exception:  # noqa: BLE001 - degrade honestly, never fail the turn over this
            logger.warning("pending cart_proposal lookup failed", exc_info=True)
            pending = []
        if pending:
            # Most recent first - a confirmation/cancellation/correction
            # always means the LAST thing proposed, never an older one
            # still sitting PENDING behind it.
            result["pending_cart_proposal"] = pending[-1]
    return result


def _has_new_shopping_content(delta) -> bool:
    """True when THIS turn's Stage 1 extraction contains anything a topic
    could plausibly be about - a new goal, or a new constraint/must-have/
    nice-to-have/exclusion/priority. Used only to decide whether the Focus
    Stack transition procedure has anything to reason about this turn -
    never to guess WHAT the content means, only whether there is any."""
    return bool(
        delta.goal
        or delta.constraints_add
        or delta.must_have_add
        or delta.nice_to_have_add
        or delta.exclusions_add
        or delta.priorities_add
    )


def _invalidate_incompatible_fulfillment(
    new_state: ShoppingStateV2, target_product_ids: set[str],
) -> bool:
    """Fulfillment-lifecycle fix (2026-09): a real topic switch (a genuine
    "resume"/"push", never a plain "continue") must never leave a still-
    pending fulfillment question (`needs_variant`/`pending`) dangling for
    a DIFFERENT topic than the one now active - live-reproduced: "Go back
    to the running shoes" left 8 wrong-topic (shoe) fulfillment items
    silently pending in the background after the shopper had already
    tried to cancel them, with no code path that ever cleared or expired
    them - any later message that happened to look like a plausible
    variant answer could have resumed answering a stale add nobody asked
    for anymore. Only ever drops items whose `product_id` is NOT part of
    the topic becoming active (a push starts with no real product_ids
    yet, so everything still pending is incompatible with it by
    definition) - never touches an item already `"added"` (a real cart
    write already happened) or `"skipped"` (an explicit decline already
    recorded); those are history, not live state to invalidate."""
    items = list(new_state.fulfillment_queue)
    kept = [
        it for it in items
        if it.status in ("added", "skipped") or it.product_id in target_product_ids
    ]
    if len(kept) == len(items):
        return False
    new_state.fulfillment_queue = kept
    return True


async def node_topic_transition(state: ShopperTurnState) -> dict:
    """Decides and applies this turn's Focus Stack transition - see
    `topic_transition.py`'s own module docstring for the full contract.
    Runs once, right after `apply_delta`, before ANY capability (including
    mission) gets a turn - every later node reads `state["topic_decision"]`
    rather than re-deriving it.

    Only "resume" is fully applied and persisted here (the stack SHAPE
    changes - who's active/paused - is already fully known). "push" is
    applied to the stack shape here too, but the new topic's real
    `product_ids` are attached later, by whichever node actually discovers
    them (`node_finalize` for discovery, `node_mission` for a mission) -
    the same "shape now, content when known" split `active_products`
    itself already uses. "continue" and "ambiguous_mission_check" make no
    stack-shape change at all; the latter is finalized by `node_mission`
    once it knows whether the mission genuinely claimed this turn."""
    new_state = state["new_state"]
    delta = state["delta"]

    # Production architecture (2026-09), Phase 1 reconciliation: the
    # turn's real interpretation (`node_understand`, which runs BEFORE
    # this node now) is authoritative over the blind guess below whenever
    # it confidently resolved a "resume" against a REAL, different paused
    # topic - consulted here, before anything is committed, rather than
    # patched afterward (the old `_reconcile_authoritative_resume` call
    # site in `node_route_capability`, which ran only after THIS node had
    # already blindly committed something else - see that function's own
    # docstring for the exact staleness bug this closes at the source).
    # Same function, same checks (never invents a target, never fires for
    # a legacy-bridged synthetic topic, never re-fires if already active) -
    # only its calling time changed.
    verified_interpretation = state.get("verified_interpretation")
    authoritative_resume = (
        _reconcile_authoritative_resume(new_state, verified_interpretation)
        if verified_interpretation is not None else None
    )
    if authoritative_resume is not None:
        await shopping_state_store.merge(
            state["session_id"], state["connection_id"],
            focus_stack=[t.model_dump(mode="json") for t in new_state.focus_stack],
            fulfillment_queue=[i.model_dump(mode="json") for i in new_state.fulfillment_queue],
        )
        return {"new_state": new_state, "topic_decision": authoritative_resume, "new_topic_id": None}

    decision = decide_transition(
        focus_stack=new_state.focus_stack,
        message=state["message"],
        delta_goal=delta.goal,
        has_new_shopping_content=_has_new_shopping_content(delta),
    )
    # Defer to `node_mission`'s own topic creation when this exact turn is
    # about to independently trigger a BRAND-NEW mission
    # (`_has_new_mission_shape`, `_edge_after_fulfillment_check`'s own
    # first condition) - pushing an ordinary "discovery" topic here first
    # would only leave an orphaned, empty topic behind once the mission
    # claims the turn instead and creates its own `kind="mission"` topic.
    mission_topic_exists = any(t.kind == "mission" for t in new_state.focus_stack)
    if (
        decision.action == "push"
        and not mission_topic_exists
        and _has_new_mission_shape(state["message"])
    ):
        return {"new_state": new_state, "topic_decision": decision, "new_topic_id": None}
    # Rev. 2 architecture (2026-09), gap fix: a correction/cancellation/
    # pause-shaped message ("Actually I meant the shoes", "No, forget
    # that") must never push a new topic labeled with its own raw
    # sentence - live-reproduced: exactly that happened, producing a
    # topic literally named "Actually I meant the shoes." that then
    # leaked into a later "what else do you have" reply. This node runs
    # BEFORE `route_capability`'s real interpretation call, so it cannot
    # yet know `question_type`/`conversational_repair` - deferring here
    # (same mechanism as the mission-shape deferral just above: skip the
    # push, let the turn continue on whatever topic is already active)
    # is the safe default until the real interpretation runs. A message
    # that turns out to be a genuine new topic starting with one of these
    # words is rare and, worst case, stays attached to the prior topic
    # rather than getting a nonsense label - a strictly safer failure mode.
    if decision.action == "push" and looks_like_state_change_shape(state["message"]):
        return {"new_state": new_state, "topic_decision": decision, "new_topic_id": None}
    new_topic_id: str | None = None
    if decision.action == "resume":
        new_stack = apply_transition(focus_stack=new_state.focus_stack, decision=decision)
        new_state.focus_stack = new_stack
        target = next((t for t in new_stack if t.topic_id == decision.topic_id), None)
        fulfillment_changed = _invalidate_incompatible_fulfillment(
            new_state, set(target.product_ids) if target else set(),
        )
        merge_kwargs = {"focus_stack": [t.model_dump(mode="json") for t in new_stack]}
        if fulfillment_changed:
            merge_kwargs["fulfillment_queue"] = [
                i.model_dump(mode="json") for i in new_state.fulfillment_queue
            ]
        await shopping_state_store.merge(state["session_id"], state["connection_id"], **merge_kwargs)
    elif decision.action == "push":
        new_topic_id = uuid.uuid4().hex[:12]
        # Rev. 2 migration (2026-09), live-reproduced serious bug: `delta.goal`
        # is documented (`prompts.py`'s own schema) as "what they are
        # ultimately trying to buy... the CATEGORY/mission itself" - a
        # durable, SESSION-level running theme the model is free to
        # describe broadly (observed live: "coffee grinder" for BOTH a
        # coffee topic and a later, separate grinder topic; "running
        # shoes" for BOTH a shoes topic and a later, separate socks
        # topic) - never a promise that it names THIS specific turn's own
        # new topic. Every migrated capability's cross-topic
        # disambiguation (`verification.py`'s label matching and its
        # cross-check) depends on each topic having its OWN distinct,
        # turn-accurate label - two topics silently sharing one stale
        # label broke that at the root, independent of anything else this
        # migration fixed.
        #
        # Grounding fix (2026-09): the raw shopper sentence was used here
        # directly as the label - genuinely distinct per turn, but never a
        # CATEGORY name, and later reused verbatim as a literal catalog
        # search string by `run_alternatives`/`retrieve` (live-reproduced:
        # "Actually I meant the shoes" as a topic label produced a
        # "Thermal Half Zip" - an unrelated apparel item - in an
        # "alternatives" answer, because the search engine matched
        # whatever loosely overlapped that sentence). No real product is
        # known yet at THIS point in the turn (topic_transition runs
        # before retrieval) - there is nothing real yet to build a
        # semantic label FROM, so this starts empty, exactly the same
        # "no real label yet" state the legacy-bridge synthetic topics
        # already use (`conversation_context.py::build_context`'s own
        # docstring; `_effective_label`/`_paused_topic_dict` already
        # degrade an empty label to real product titles). `node_finalize`
        # backfills a REAL, product-grounded label the moment this turn's
        # own search verifies real products for this topic - see its own
        # comment at the backfill site.
        new_stack = apply_transition(
            focus_stack=new_state.focus_stack, decision=decision,
            new_topic_id=new_topic_id, new_topic_kind="discovery",
            new_topic_label="",
        )
        new_state.focus_stack = new_stack
        fulfillment_changed = _invalidate_incompatible_fulfillment(new_state, set())
        merge_kwargs = {"focus_stack": [t.model_dump(mode="json") for t in new_stack]}
        if fulfillment_changed:
            merge_kwargs["fulfillment_queue"] = [
                i.model_dump(mode="json") for i in new_state.fulfillment_queue
            ]
        await shopping_state_store.merge(state["session_id"], state["connection_id"], **merge_kwargs)
    return {"new_state": new_state, "topic_decision": decision, "new_topic_id": new_topic_id}


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
        reasoning=state["reasoning"], shopper_id=state.get("shopper_id"),
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
            "cart_changed": bool(added_ids),
            "fulfillment_pending": _fulfillment_pending_dict(new_state),
        },
    )
    return {"trace": trace, "new_state": new_state, "result": result}


def edge_after_fulfillment_continue(state: ShopperTurnState) -> str:
    if state.get("result") is not None:
        return END
    return "topic_transition"


def _is_pure_confirmation(verified: VerifiedInterpretation, delta) -> bool:
    """A model-claimed "confirmation" may execute a pending commerce
    action ONLY when it is the whole message: no new search, no product
    reference, no question, no cart operation, no secondary operation and
    no new shopping content riding along. "ok, what else do you have?"
    is a continuation with a question in it, not a yes - if the model
    tags it "confirmation" anyway, that tag alone must not touch the
    cart. Structural (reads the interpretation's own other fields), not
    a word list."""
    if verified.question_type not in ("retrieve", "state_change"):
        return False
    if verified.cart_operation not in (None, "none", ""):
        return False
    if verified.secondary_operation is not None:
        return False
    if verified.resolved_reference is not None and verified.resolved_reference.product_ids:
        return False
    if verified.capability not in ("conversational", "unknown", "cart_action", "discover"):
        return False
    if delta is not None and (
        getattr(delta, "goal", None) or getattr(delta, "constraints_add", None)
        or getattr(delta, "must_have_add", None) or getattr(delta, "nice_to_have_add", None)
        or getattr(delta, "exclusions_add", None) or getattr(delta, "priorities_add", None)
    ):
        return False
    return True


async def node_cart_proposal_continue(state: ShopperTurnState) -> dict:
    """Production architecture (2026-09), Phase 3: the missing third
    PendingAction case. `edge_after_apply_delta` only routes here when a
    real PENDING CartProposal exists AND this turn's own, already-computed
    `verified_interpretation` (the SAME single interpretation authority
    every other node reads - no second call made here) reads as
    confirming/cancelling/correcting it. `handled=False` (nothing set here)
    falls through to ordinary routing for this SAME message, exactly like
    `node_fulfillment_continue`'s own `handled` contract - a message that
    turns out not to be about the pending proposal after all is never
    trapped answering a question nobody asked."""
    trace = state["trace"]
    new_state = state["new_state"]
    verified: VerifiedInterpretation | None = state.get("verified_interpretation")
    proposal = state["pending_cart_proposal"]
    repair = verified.conversational_repair if verified is not None else "none"

    if repair == "confirmation" and _is_pure_confirmation(verified, state.get("delta")):
        try:
            core_result = await accept_cart_proposal_core(
                connection_id=state["connection_id"], session_id=state["session_id"],
                cart_id=state["cart_id"], proposal_id=proposal["proposal_id"],
                shopper_id=state.get("shopper_id"),
            )
        except HTTPException:
            # A real race (already accepted/rejected/expired elsewhere,
            # e.g. a REST tap on the same proposal moments earlier) -
            # honest, never a crash, never a fabricated success.
            return {
                "trace": trace,
                **_terminal_answer(trace, new_state, "Looks like that's already been taken care of."),
            }
        trace.response_used_model = False
        trace.finish()
        result = TurnResultV2(
            reply=core_result.reply, state=new_state,
            evidence=TurnEvidence(query=None, products=[], no_match_reason=None),
            used_model=False,
            trace={
                **trace.as_dict(),
                "cart_proposal_id": proposal["proposal_id"],
                "cart_changed": core_result.cart_changed,
            },
        )
        return {"trace": trace, "new_state": new_state, "result": result}

    if repair in ("cancellation", "correction"):
        try:
            await cart_proposals_store.set_state(
                state["connection_id"], proposal["proposal_id"], state="REJECTED",
            )
        except Exception:  # noqa: BLE001 - degrade honestly, never fail the turn over this
            logger.warning("cart_proposal cancellation failed for %r", proposal.get("proposal_id"), exc_info=True)
        if repair == "cancellation":
            return {
                "trace": trace,
                **_terminal_answer(trace, new_state, "No problem - I won't do that."),
            }
        # "correction": the stale proposal is withdrawn, silently - this
        # turn's own message (a fresh cart-shaped ask, already resolved by
        # the SAME interpretation call above) falls through to ordinary
        # routing below, exactly as if no proposal had ever been pending -
        # never a second, independent interpretation of it.
        return {"trace": trace, "new_state": new_state}

    # Not actually about the pending proposal (an unrelated message that
    # happened to arrive while one was still open) - fall through.
    return {"trace": trace, "new_state": new_state}


def edge_after_cart_proposal_continue(state: ShopperTurnState) -> str:
    if state.get("result") is not None:
        return END
    return "topic_transition"


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
    # A typed "yes" / "approve it" (the whole message) over this session's own
    # pending proposal(s): handled deterministically by `node_typed_confirm`, which
    # reuses the existing accept core. Below a pending size question (the branch
    # above) and above ordinary routing.
    _tc = state.get("typed_confirm")
    if (
        friction is None and _tc is not None
        and state.get("decision") is not None and state.get("gate") is not None
        and state.get("execution") is not None
    ):
        _pending = _tc["pending"]
        if _pending:
            if _tc["kind"] == "add_pointer":
                _view = current_view_product_ids(new_state)
                if _view and not set(_view) <= {p.get("product_id") for p in _pending}:
                    pass  # "add it" could mean something else on screen: ordinary add handling
                else:
                    return "typed_confirm"
            elif new_state.pending_question:
                pass  # another question is open: a bare "yes" is not clearly about the proposal
            else:
                return "typed_confirm"
        elif _tc["kind"] == "confirm" and not new_state.pending_question:
            return "typed_confirm"
    # Production architecture (2026-09), Phase 3: second precedence tier -
    # a real, PENDING CartProposal exists for this cart (fetched once,
    # cheaply, in `node_apply_delta`) and THIS turn's own interpretation
    # (already computed in `node_understand` - no second call here) reads
    # as confirming/cancelling/correcting it. Below fulfillment_queue
    # (a fulfillment question is a MORE direct, MORE recent question than
    # an older standing proposal) but above ordinary topic/capability
    # routing, exactly the same reasoning `fulfillment_continue` already
    # uses above.
    if (
        friction is None
        and state.get("pending_cart_proposal") is not None
        and state.get("decision") is not None
        and state.get("gate") is not None
        and state.get("execution") is not None
    ):
        return "cart_proposal_continue"
    # A guided mission is open and this is a short instruction about it
    # ("skip the socks", "what's next", "keep the shoes under 3000"): handled
    # deterministically by `node_guided_turn`, which hands anything it cannot
    # settle straight back to `topic_transition`.
    if (
        friction is None
        and guided_mission.classify_command(state.get("message") or "", new_state) is not None
    ):
        return "guided_turn"
    return "topic_transition"


async def node_guided_turn(state: ShopperTurnState) -> dict:
    """Skip / next / budget change for an open guided mission (see
    `guided_mission.py`). Reads the real cart to confirm what is done, never
    mutates it. Declines (returns `{}` and routes on to ordinary handling)
    whenever the command cannot be applied to a real stage."""
    new_state = state["new_state"]
    trace = state["trace"]
    command = guided_mission.classify_command(state["message"], new_state)
    turn = None
    if command is not None:
        turn = await guided_mission.handle_command(
            adapter=state["adapter"], command=command, message=state["message"],
            state=new_state, cart_id=state.get("cart_id"),
            memory_facts=state.get("memory_facts"),
        )
    if turn is None:
        return {"guided_declined": True}
    final_state = turn.state or new_state
    mission_topic = next((t for t in final_state.focus_stack if t.kind == "mission"), None)
    if mission_topic is not None and turn.view_ids:
        await _record_product_set(
            session_id=state["session_id"], connection_id=state["connection_id"],
            new_state=final_state, trace=trace, source_capability="mission",
            verified_ids=turn.view_ids, target_topic_id=mission_topic.topic_id,
        )
    trace.capability = "mission"
    trace.basket_budget = turn.budget_trace
    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=turn.reply, state=final_state,
        evidence=TurnEvidence(query=None, products=[], no_match_reason=None),
        used_model=False, trace=trace.as_dict(), products=turn.products,
    )
    return {"new_state": final_state, "trace": trace, "result": result}


async def node_typed_confirm(state: ShopperTurnState) -> dict:
    """A typed "yes" / "approve it" / "add it" as the shopper's approval of the ONE
    proposal waiting on their cart. It reuses `accept_cart_proposal_core` - the very
    function the Accept button's route runs - so staleness, stock/price re-checks,
    the atomic PENDING->ACCEPTED claim (a replay finds nothing pending and adds
    nothing), the risk gate, execution and the variant requirement are all
    unchanged. With nothing waiting, or several waiting, nothing is executed:
    the shopper is told so / asked which."""
    trace = state["trace"]
    new_state = state["new_state"]
    pending = state["typed_confirm"]["pending"]

    def _done(text: str, extra: dict | None = None) -> dict:
        trace.capability = "cart_action"
        trace.response_used_model = False
        trace.finish()
        return {"trace": trace, "result": TurnResultV2(
            reply=text, state=new_state,
            evidence=TurnEvidence(query=None, products=[], no_match_reason=None),
            used_model=False, trace={**trace.as_dict(), **(extra or {})},
        )}

    if not pending:
        return _done("There's nothing waiting for your approval right now.")

    if len(pending) > 1:
        lines = []
        for i, prop in enumerate(pending[-5:], start=1):
            title = prop.get("product_id") or "an item"
            if prop.get("change_type") != "CLEAR":
                try:
                    title = (await state["adapter"].get_product(prop["product_id"])).title
                except Exception:  # noqa: BLE001 - fall back to the id, never raise
                    logger.warning("typed confirm: get_product failed", exc_info=True)
            verb = {"ADD": "Add", "REMOVE": "Remove", "CHANGE_QUANTITY": "Change the quantity of",
                    "CLEAR": "Clear your cart"}.get(prop.get("change_type"), "Change")
            lines.append(f"{i}. {verb} {title}" if prop.get("change_type") != "CLEAR" else f"{i}. {verb}")
        return _done(
            f"You have {len(pending)} suggestions waiting: " + "; ".join(lines)
            + ". Which one should I approve? Use Accept on the one you want in the "
              "Suggestions panel - nothing changes until you do."
        )

    proposal = pending[0]
    try:
        core = await accept_cart_proposal_core(
            connection_id=state["connection_id"], session_id=state["session_id"],
            cart_id=state["cart_id"], proposal_id=proposal["proposal_id"],
            shopper_id=state.get("shopper_id"),
        )
    except HTTPException:
        # Lost a race (accepted/rejected elsewhere a moment ago): honest, no change.
        return _done("Looks like that's already been taken care of.")
    return _done(core.reply, {
        "cart_proposal_id": proposal["proposal_id"],
        "cart_changed": core.cart_changed,
        # A product that still needs a size/option: the existing accept-flow
        # buttons (the shopper's own tap adds it), exactly as in the panel.
        "accept_choices": list(core.choices or []),
    })


def edge_after_guided_turn(state: ShopperTurnState) -> str:
    if state.get("result") is not None:
        return END
    return "topic_transition"


def _has_new_mission_shape(message: str) -> bool:
    """The ORIGINAL, unchanged "does this look like a brand-new multi-item
    mission starting" signal - preserved exactly so starting a genuinely
    new mission ("I need shoes, socks and a hydration bottle") keeps
    working exactly as it always has. This is deliberately NOT retired in
    favor of `topic_transition.py`'s own decision procedure: that
    procedure reasons about topics ALREADY in the Focus Stack (continue/
    push/resume an existing one) - a mission that doesn't exist yet has no
    topic to reason about, so this earlier, orthogonal signal is still
    what triggers trying `interpret_mission_items` for the FIRST time in a
    conversation."""
    return any(sep in message.lower() for sep in (",", " and ", " & "))


def _edge_after_fulfillment_check(state: ShopperTurnState) -> str:
    """The mission/tradeoff/route_capability decision. Two independent
    ways `interpret_mission_items` gets a turn, deliberately NOT merged
    into one heuristic - see `_has_new_mission_shape`'s own docstring:

    1. No mission topic exists in the Focus Stack yet, and this message
       has the classic multi-item shape - try starting one, exactly as
       this codebase always has.
    2. A mission topic DOES exist somewhere in the stack, and
       `topic_transition.py`'s own decision procedure (already run in
       `node_topic_transition`, before this edge) says the mission is
       either already active (rule 5 - always allowed) or genuinely
       ambiguous whether this turn belongs to it (rule 3's tiebreaker) -
       the exact structural fix for the mission-hijacking-every-turn bug:
       an unrelated turn no longer reaches `interpret_mission_items` at
       all unless one of these two well-defined conditions holds.
    """
    new_state = state["new_state"]
    friction = state.get("friction")
    decision = state.get("topic_decision")
    mission_topic_in_stack = any(t.kind == "mission" for t in new_state.focus_stack)
    # Backward-compatibility bridge: a session with real, persisted
    # `mission_items` from before the Focus Stack existed has no
    # corresponding `kind="mission"` Topic yet - never silently drop an
    # in-progress mission for a returning shopper just because the
    # migration hadn't backfilled a Topic for it. `node_mission` creates
    # the missing Topic the next time it runs (self-healing); until then,
    # this session behaves exactly as it did before this change.
    legacy_mission_without_topic = bool(new_state.mission_items) and not mission_topic_in_stack
    # Structural gap fix (2026-09): `_has_new_mission_shape`'s own
    # "contains a separator word" check has no awareness of whether a
    # real, per-turn interpretation already ran and produced a genuine
    # judgment this turn - live-reproduced: "Compare the cheapest and the
    # lightest" (a real `question_type` a real interpretation call
    # correctly recognizes on MOST but not all repeated calls) still fell
    # into "I'm now tracking: cheapest, lightest" on the calls where the
    # SEPARATE, per-turn classification didn't confirm a routing claim -
    # this heuristic has no way to know a real answer was already
    # attempted, so it treated an unconfirmed miss as license to guess a
    # brand-new multi-item mission instead. `verified_interpretation`
    # being present at all means a real interpretation call ran THIS turn
    # and produced a real, checkable judgment (even an unconfirmed
    # `question_type == "retrieve"` is a real judgment: "this is not a
    # question, treat it as an ordinary search") - the multi-item-mission
    # guess is a fallback for when NO real judgment exists at all, never a
    # second opinion overriding one that already ran.
    verified_interpretation = state.get("verified_interpretation")
    if friction is None and state.get("reasoning") is not None and (
        legacy_mission_without_topic
        or (
            not mission_topic_in_stack
            and verified_interpretation is None
            and _has_new_mission_shape(state["message"])
        )
        or (decision is not None and decision.mission_call_allowed)
    ):
        return "mission"
    return edge_after_mission_check(state)


def edge_after_mission_check(state: ShopperTurnState) -> str:
    # Rev. 2 migration (2026-09): the old trigger here - "route straight
    # to tradeoff whenever THIS turn's own delta carried any
    # `priorities_add` at all" - is RETIRED, not merely bypassed. It was
    # topic-blind in a different, more serious way than the cart-action
    # bug this session already fixed: `delta.priorities_add` has no
    # freshness guarantee (the primary `understand()` call can and does
    # re-emit an old priority on turns that never restated it - live-
    # reproduced: "go back to the running shoes" and "compare the
    # cheapest and the lightest" both hijacked into a stale re-ranking
    # reply with no priority language at all). `question_type ==
    # "attribute_refinement"` (from `node_route_capability`'s own real,
    # per-turn interpretation call) is the reliable replacement signal -
    # it now routes through `Capability.ANSWER_QUESTION` /
    # `node_answer_question`, which calls the SAME `run_tradeoff_turn`
    # domain logic (unchanged, still legitimate re-ranking
    # infrastructure), just gated on a trustworthy per-turn signal
    # instead of a raw, non-fresh delta field. `route_capability` already
    # ran earlier (see the precedence order:
    # topic_transition -> route_capability -> [cart-check] -> mission/
    # tradeoff) - a turn falling through mission/tradeoff dispatches
    # straight to capability_dispatch using the `capability` it already
    # computed, never a second, redundant recomputation.
    return "capability_dispatch"


async def _record_product_set(
    *,
    session_id: str,
    connection_id: str,
    new_state: ShoppingStateV2,
    trace: TurnTrace,
    source_capability: str,
    verified_ids: list[str],
    target_topic_id: str | None = None,
) -> None:
    """Production architecture (2026-09), Phase 0: the single, universal
    writer for "what's currently in view" - the `active_products`/
    `recent_product_groups` ledger, `LastRendered`, and (when a real topic
    is active) that topic's own `product_ids`. Every capability that
    resolves/shows a concrete product set calls this instead of writing any
    of these fields directly.

    Before this existed, only `node_finalize` (discovery) and
    `node_tradeoff`/`node_mission` (via their own duplicated, hand-rolled
    copies of this exact write) ever recorded what a turn showed - compare,
    recommendation, explanation, alternatives, compatibility, and
    complementary all resolved and displayed real products but persisted
    nothing, so a later reference ("add it" after a recommendation) had no
    durable record to resolve against. This closes that gap generally,
    not per-capability, and gives every writer its own correct
    `source_capability` tag instead of `node_finalize`'s previously
    hardcoded `"discovery"`.

    Never overwrites with an empty list - a turn that found nothing real
    leaves whatever was previously recorded untouched, the same "never
    erase real state with a failed turn's nothing" discipline
    `node_finalize`'s own discovery path already established.
    """
    if not verified_ids:
        return
    groups = list(new_state.recent_product_groups)
    if not groups or groups[-1] != verified_ids:
        groups.append(verified_ids)
    groups = groups[-_LEDGER_MAX_GROUPS:]
    last_rendered = LastRendered(
        product_ids=verified_ids, source_capability=source_capability,
        source_topic_id=target_topic_id, turn_id=trace.turn_id,
    )
    new_state.active_products = verified_ids
    new_state.recent_product_groups = groups
    new_state.last_rendered = last_rendered
    merge_kwargs: dict[str, Any] = {
        "active_products": verified_ids,
        "recent_product_groups": groups,
        "last_rendered": last_rendered.model_dump(mode="json"),
    }
    if target_topic_id is not None:
        topic_touched = False
        for t in new_state.focus_stack:
            if t.topic_id == target_topic_id:
                t.product_ids = verified_ids
                topic_touched = True
                break
        if topic_touched:
            merge_kwargs["focus_stack"] = [t.model_dump(mode="json") for t in new_state.focus_stack]
    await shopping_state_store.merge(session_id, connection_id, **merge_kwargs)


async def _execute_secondary_operation(
    *,
    secondary_op,
    primary_products: list,
    adapter,
    reasoning,
    message: str,
    new_state: ShoppingStateV2,
) -> str | None:
    """Production architecture (2026-09), Phase 5: compound turns. Executes
    a SECOND, genuinely different operation a turn's message also asked
    for ("show me X, and how does that compare to Y"), using an INTRA-TURN
    handle (`primary_products` - THIS turn's own, already-verified results,
    passed as a plain in-memory argument) - never a second, persisted
    `ConversationContext` read, and never a second call to the turn's
    single interpretation authority (the secondary operation was already
    resolved as part of the SAME primary interpretation call). Returns the
    secondary answer's own reply text to append to the primary reply, or
    `None` when it genuinely cannot be resolved - honest silence, never a
    guessed answer appended."""
    if not primary_products:
        return None
    candidates = list(primary_products[:5])
    if secondary_op.reference_names:
        named_query = " ".join(secondary_op.reference_names)
        try:
            live_result = await retrieve(adapter, named_query, limit=5)
        except Exception:  # noqa: BLE001 - degrade honestly, never raise
            live_result = None
        if live_result is not None and not live_result.unavailable and live_result.products:
            score, best = best_matches_with_score(live_result.products, named_query)
            if score > 0:
                known_ids = {p.product_id for p in candidates}
                candidates = candidates + [p for p in best if p.product_id not in known_ids]

    if secondary_op.capability == "recommendation":
        # `run_recommendation` already applies the real budget hard filter
        # itself (including its own honest "nothing in budget, here's the
        # closest alternative" fallback) - never double-filtered here,
        # which would silently lose that fallback's own candidate pool.
        reply_text, _evidence, _shown = run_recommendation(
            products=candidates, state=new_state, budget_amount=new_state.budget_amount,
            scope_label="what you just searched for", message=message,
        )
        return reply_text

    if secondary_op.capability == "compare":
        # Root-cause fix (2026-09): a hard budget constraint must never be
        # violated by a compound turn's secondary comparison. The
        # named-reference `retrieve()` call above has no budget awareness
        # of its own (a fresh, unfiltered catalog search) - unlike
        # `recommendation` above, nothing else here ever checks price
        # against a stated budget, so an out-of-budget named reference
        # could otherwise reach the comparison unfiltered. Unconditional -
        # never falls back to the unfiltered set even when nothing
        # qualifies; an empty/singleton result correctly reaches the
        # `len(candidates) >= 2` decline just below rather than silently
        # comparing something outside what the shopper stated.
        if new_state.budget_amount is not None:
            try:
                limit = float(new_state.budget_amount)
            except (TypeError, ValueError):
                limit = None
            if limit is not None:
                candidates = [p for p in candidates if p.price is not None and float(p.price.amount) <= limit]

    if secondary_op.capability == "compare" and len(candidates) >= 2:
        items = [
            {
                "product_id": p.product_id, "title": p.title,
                "price": str(p.price) if p.price is not None else None,
                "availability": str(p.availability), "description": p.description,
            }
            for p in candidates
        ]
        synthesis = await reasoning.synthesize(
            kind="comparison", message=message, items=items,
            shopping_state=new_state.as_prompt_dict(),
        )
        if synthesis.text and not _claims_a_cart_action(synthesis.text):
            text, _used_model = grounded_reply_or_fallback(
                cited_reason=synthesis.cited_reason, text=synthesis.text,
                products=candidates, fallback="", used_model=synthesis.used_model,
            )
            if text:
                return text
        # Deterministic fallback: names what's being compared, honestly -
        # never inventing a verdict the model didn't produce/ground.
        parts = [f"{p.title}" + (f" ({p.price})" if p.price else "") for p in candidates[:2]]
        return "Comparing " + " and ".join(parts) + "."
    return None


def _active_topic_id(state: "ShopperTurnState") -> str | None:
    """The currently active topic's id, if any - the natural
    `target_topic_id` for a capability (compare/recommend/explain/
    alternatives/compatibility/complementary) that shows products already
    being discussed rather than starting a brand-new search. Never creates
    a topic - only attaches to one that already exists."""
    context = state.get("conversation_context")
    if context is not None and context.active_topic is not None:
        return context.active_topic.topic_id
    return None


async def node_mission(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    fulfillment.ADDED_THIS_TURN.set([])
    guided_mission.BUDGET_TRACE.set(None)
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
        shopper_id=state.get("shopper_id"),
    )
    if not mission_handled:
        # Falls through to exactly where the mission-check would have been
        # skipped - same as orchestrator.py's original "if not handled,
        # keep going" shape.
        return {}
    new_state = mission_state
    trace.capability = "mission"
    trace.basket_budget = guided_mission.BUDGET_TRACE.get()
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

    # Production architecture (2026-09), Phase 0: mission's own top real,
    # already-verified candidate per item is "currently shown" the same way
    # discovery's own verified products are - written through the single,
    # shared `_record_product_set` writer (never a second, hand-rolled copy
    # of the same ledger/LastRendered/topic-content write) so every module
    # that reads this state (compare/cart-action/tradeoff/reference
    # resolution) sees mission-tracked products correctly, tagged
    # `source_capability="mission"`.
    verified_ids = [item.candidates[0] for item in new_state.mission_items if item.candidates]
    # A guided mission shows one stage at a time: "the first one"/"compare
    # both"/"add the Cloudstep" refer to the CURRENT stage's options.
    guided_view = guided_mission.view_product_ids(new_state)
    if guided_view:
        verified_ids = guided_view

    # Focus Stack finalization - the mission Topic frame's own product_ids
    # are written by the shared helper below, from the SAME verified_ids,
    # never a second, independently-derived list. Three cases:
    #
    # 1. `topic_decision.action == "ambiguous_mission_check"` - this turn's
    #    mission-relevance was genuinely undecidable structurally
    #    (topic_transition.py's own rule 3) and `interpret_mission_items`
    #    (just run, above) resolved it as handled=True - finalize as a
    #    RESUME of the named mission topic now that the ambiguity is
    #    settled, demoting whatever was active in its place.
    # 2. No mission topic exists in the stack at all yet (a brand-new
    #    mission, OR a legacy session healing forward per
    #    `_edge_after_fulfillment_check`'s own bridge comment) - PUSH one.
    # 3. Otherwise (the mission topic was already active - the ordinary,
    #    most common case, rule 5's unconditional path) - only its
    #    product_ids need refreshing in place, no stack-shape change.
    decision = state.get("topic_decision")
    mission_topic = next((t for t in new_state.focus_stack if t.kind == "mission"), None)
    if decision is not None and decision.action == "ambiguous_mission_check":
        stack = apply_transition(focus_stack=new_state.focus_stack, decision=decision)
        mission_topic = next((t for t in stack if t.topic_id == decision.topic_id), None)
        new_state.focus_stack = stack
    elif mission_topic is None:
        new_topic_id = state.get("new_topic_id") or uuid.uuid4().hex[:12]
        stack = apply_transition(
            focus_stack=new_state.focus_stack,
            decision=TransitionResult("push", None, False),
            new_topic_id=new_topic_id, new_topic_kind="mission",
            new_topic_label=", ".join(i.label for i in new_state.mission_items)[:120] or "mission",
            new_topic_mission_item_ids=[i.item_id for i in new_state.mission_items],
        )
        mission_topic = next(t for t in stack if t.topic_id == new_topic_id)
        new_state.focus_stack = stack

    if mission_topic is not None:
        # Topic shape/identity (push/resume) and mission_item_ids are
        # persisted unconditionally - a mission turn with no item having a
        # real candidate yet still needs its topic and item tracking saved.
        # `_record_product_set` below handles product_ids/ledger/
        # LastRendered separately and only when there is something real to
        # record (never erasing with an empty list).
        mission_topic.mission_item_ids = [i.item_id for i in new_state.mission_items]
        await shopping_state_store.merge(
            state["session_id"], state["connection_id"],
            focus_stack=[t.model_dump(mode="json") for t in new_state.focus_stack],
        )
        await _record_product_set(
            session_id=state["session_id"], connection_id=state["connection_id"],
            new_state=new_state, trace=trace, source_capability="mission",
            verified_ids=verified_ids, target_topic_id=mission_topic.topic_id,
        )

    trace.finish()
    result = TurnResultV2(
        reply=mission_reply,
        state=new_state,
        evidence=evidence,
        used_model=False,
        trace={
            **trace.as_dict(), "mission_proposal_ids": mission_proposal_ids,
            "cart_changed": bool(fulfillment.ADDED_THIS_TURN.get()),
        },
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
        await _record_product_set(
            session_id=state["session_id"], connection_id=state["connection_id"],
            new_state=new_state, trace=trace, source_capability="trade_off",
            verified_ids=reordered_ids, target_topic_id=_active_topic_id(state),
        )
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

def _reconcile_authoritative_resume(
    new_state: ShoppingStateV2, verified_interpretation: VerifiedInterpretation,
) -> TransitionResult | None:
    """Sequencing fix (2026-09): `node_topic_transition` commits its own
    stack-shape decision BEFORE any real interpretation exists this turn -
    it can only ever guess from raw message/label-overlap
    (`decide_transition`'s own rules). Once a real, grounded
    interpretation HAS run (this function's caller, `node_route_capability`,
    the same real `verify()` call every other authoritative decision this
    turn already reads), its own `context_action`/`context_target_topic_id`
    is the authoritative answer to "which topic is this" - never
    subordinate to the earlier blind guess. Live-reproduced gap this
    closes: "Go back to the running shoes" resolved `context_action=
    "resume"` against the real, already-paused shoes topic, but
    `topic_transition` (running first, blind) had already committed
    "continue" on a DIFFERENT topic - nothing ever corrected the stack
    shape to match the real resolution, so `node_finalize`'s own
    unrelated category-mismatch self-heal pushed a brand-new DUPLICATE
    topic instead of resuming the real one.

    Only ever re-points which REAL topic is active - never invents one,
    never touches any topic's `product_ids` (that stays `node_finalize`'s
    job, unchanged), and never fires for a legacy-bridged synthetic topic
    (`topic_id` starting `"legacy:"` has no real Focus Stack entry to
    re-point to). Returns the corrected `TransitionResult` for the caller
    to hand to `node_finalize` in place of the earlier blind one, or
    `None` when nothing needed correcting."""
    if verified_interpretation.context_action != "resume":
        return None
    target_id = verified_interpretation.context_target_topic_id
    if not target_id or target_id.startswith("legacy:"):
        return None
    target = next((t for t in new_state.focus_stack if t.topic_id == target_id), None)
    if target is None or target.state == "active":
        return None
    decision = TransitionResult("resume", target_id, mission_call_allowed=target.kind == "mission")
    new_state.focus_stack = apply_transition(focus_stack=new_state.focus_stack, decision=decision)
    _invalidate_incompatible_fulfillment(new_state, set(target.product_ids))
    return decision


async def node_route_capability(state: ShopperTurnState) -> dict:
    """Production architecture (2026-09), Phase 1 reconciliation: this node
    no longer attempts any interpretation call itself - `node_understand`
    is now the sole call site (see that node's own docstring). This node
    only consumes `state["verified_interpretation"]` (already computed and
    verified) to refine the capability decision, and applies the remaining
    deterministic backstops (preference-status/cart-intelligence/compare-
    confirm) exactly as before."""
    friction = state.get("friction")
    capability_confirmed_reference = False
    # Already computed and verified by `node_understand` - read once here,
    # refined below (never re-derived, never re-called).
    verified_interpretation = state.get("verified_interpretation")
    if friction == "PAYMENT_DECLINED":
        capability = Capability.PAYMENT_RECOVERY
    elif friction == friction_recovery.DEAD_SEARCH and state.get("friction_query"):
        # A dead search that arrived WITH its term: run the ordinary
        # discovery pipeline on that term (vocabulary normalization,
        # category-grounded browse). Routed deterministically off the
        # explicit friction signal - search-bar text is never a command, so
        # nothing here consults the model's capability guess. Falls back to
        # the department list in `node_finalize` when nothing is found.
        capability = Capability.DISCOVER
    elif friction in (friction_recovery.DEAD_SEARCH, friction_recovery.PROMOTION_FAILED):
        capability = Capability.FRICTION_RECOVERY
    else:
        capability = route(state["hinted_action_type"], state.get("message_intent"))

        # Rev. 2 architecture (2026-09), Phase 9 (routing-level cutover):
        # generalizes the compare-only backstop below to EVERY capability
        # at once, rather than duplicating a per-capability cutover for
        # mission/compatibility/complementary/trade_off/checkout/
        # cart_status individually - all six still route entirely through
        # `SOFT_CAPABILITIES`/`route()`'s single unconfirmed model guess
        # today. Tried FIRST, only when the guess was SOFT (unconfirmed)
        # and the message looks reference-shaped (the same cheap
        # `looks_like_reference` prefilter already used below - never
        # itself the decision) and isn't an explicit cart command (that
        # has its own separate, authoritative check). `claim.capability`
        # is the SAME genuine interpretation already proven live in
        # Phases 2/4/7 - a real answer to "what kind of help does this
        # need," grounded in the real conversation, not a second guess
        # layered on the first. Falls through to the existing, unchanged
        # narrow backstop immediately below whenever this doesn't apply
        # or doesn't produce a recognized capability - nothing here
        # removes that path, only tries a broader one first.
        # Architecture-level fix (2026-09): the entry gate used to ALSO
        # require `delta.priorities_add`/`nice_to_have_add` non-empty for a
        # bare declarative preference statement ("lightweight matters most
        # to me") with no "?" and no interrogative opener. Measured live:
        # that field is itself unreliable per turn (0/4 in one repeated
        # sample of the exact same input) - on the turns it comes back
        # empty, the interpretation call was never even ATTEMPTED, so no
        # downstream signal (including the reliable
        # `durable_fact_candidates` check below) ever got a chance to
        # fire. The real, robust fix is not a better prefilter - a
        # prefilter is inherently a guess at whether asking is worth it -
        # it is to stop gating on a guess at all once there is real
        # context to reason about: any real, already-active product set
        # is reason enough to spend the ONE bounded interpretation call
        # this turn, matching the Rev. 2 architecture's own original
        # intent (genuine per-turn understanding) rather than a cheap
        # prefilter's approximation of it.
        context: ConversationContext = state.get("conversation_context")
        routing_claim_applied = False
        # "compare both" / "compare these two" with two or more products really
        # in view and no second side named IS a comparison of that group,
        # whatever the interpretation claim says about relationship sides: a
        # plural pointer the model failed to resolve must never end in "I can
        # see one side of that". `compare` itself still resolves the exact
        # products from real structure and declines honestly if it cannot.
        collective_compare = (
            looks_like_collective_comparison(state["message"])
            and capability in SOFT_CAPABILITIES | {Capability.COMPARE, Capability.ANSWER_QUESTION}
            and len(current_view_product_ids(state["new_state"])) >= 2
        )
        if collective_compare:
            capability = Capability.COMPARE
            capability_confirmed_reference = True
            routing_claim_applied = True
        # Step 25 live finding (gpt-4o-mini): "Compare the Trailblazer and the Socks" - two products NAMED in one
        # message - was routed by the model to the relationship answer, which needs attributes on both sides and
        # replied "which other product or group did you mean?". When the shopper's own words name two or more real
        # products (or one real product and one plainly absent from the catalogue), the multi-product discovery path
        # resolves each part from the catalogue, shows every one found and says which one it could not find.
        named_multi = False
        if not collective_compare and capability in SOFT_CAPABILITIES | {Capability.COMPARE, Capability.ANSWER_QUESTION}:
            try:
                _nm_vocab = await vocabulary.get_vocabulary(state["adapter"], state["connection_id"])
                _nm = await named_product.resolve_named_products(
                    adapter=state["adapter"], message=state["message"], vocab=_nm_vocab,
                ) if _nm_vocab is not None else None
            except Exception:  # noqa: BLE001 - never break routing on this
                logger.warning("shopper_v2: named-product routing check failed", exc_info=True)
                _nm = None
            if _nm is not None and (len(_nm.products) >= 2 or (_nm.products and _nm.missing)):
                capability = Capability.DISCOVER
                named_multi = True
                routing_claim_applied = True
        # Production architecture (2026-09), Phase 1 reconciliation: the
        # turn's interpretation was already attempted and verified by
        # `node_understand` (the single authoritative call site) - this
        # node no longer attempts its own, independent interpretation call
        # at all. It only CONSUMES `state["verified_interpretation"]`
        # (already checked against real data by `verification.py::verify()`)
        # to refine the capability decision - closing Gap J's redundant-
        # call finding and Gap B's "topic/cart decisions committed before
        # interpretation runs" finding at the source, rather than patching
        # around a second call site here.
        verified_interpretation = state.get("verified_interpretation")
        if verified_interpretation is not None and not collective_compare and not named_multi:
            # Compound-turn fix (2026-09): whether THIS claim's own
            # `capability` already names a genuine, actionable operation
            # (discover/cart_action/mission/compare/...) rather than
            # nothing real to do (conversational/unknown/unset) - used
            # below to stop a correction/cancellation/pause tag from
            # steamrolling a real, separately-named operation on the SAME
            # message. Checked against the raw string the model's own
            # schema allows (`interpretation_tool()`'s own `capability`
            # enum), not the Python `Capability` enum - 'mission' is a
            # valid model output with no `Capability` member of its own
            # (routed via a separate graph edge), so parsing it through
            # `Capability(...)` would raise and wrongly read as "nothing
            # real."
            claim_has_real_operation = verified_interpretation.capability not in (
                "conversational", "unknown", "", None,
            )
            # A genuine question shape (anything but a plain "retrieve"/
            # "state_change") is authoritative over BOTH the primary
            # classifier's raw guess and the narrower capability-routing-
            # claim below - it names what kind of ANSWER is needed, which
            # routing alone can never express (this is what replaces
            # `route()`'s unconditional CONVERSATIONAL shortcut for a
            # message like "why this?", and the sticky delta-field
            # trade-off trigger for "something lightweight").
            # Step 22 (live-reproduced, OpenAI gpt-4o-mini, 3 of 3): a bare
            # recommendation imperative ("recommend one") after a comparison
            # came back from the model as a plain discovery "retrieve" - a
            # literal search for the word "recommend" - and ended in "I
            # couldn't find anything matching that search". A message whose
            # only content is a recommendation verb, with at least two real
            # products in view to choose between, is a recommendation over
            # that view, whatever the model's own unconfirmed
            # classification says (same precedence as the preference-status
            # check below). Only a plain "retrieve" is overridden - any
            # question shape the model recognised stays authoritative - and
            # only when there is a real view to recommend from; the answer
            # itself is still the grounded `run_recommendation` over exactly
            # that view, and nothing here can touch a cart.
            if (
                verified_interpretation.question_type == "retrieve"
                and looks_like_bare_recommendation_imperative(state["message"])
                and not looks_like_explicit_cart_command(state["message"])
                and len(recent_relevant_product_ids(state["new_state"], minimum=2)) >= 2
            ):
                verified_interpretation.question_type = "recommendation"
            _ref = verified_interpretation.resolved_reference
            _sec = verified_interpretation.resolved_secondary
            if (
                verified_interpretation.question_type in ("relationship", "compound_comparison")
                and _ref is not None and len(_ref.product_ids) >= 2
                and (_sec is None or not _sec.product_ids)
            ):
                # Root-cause fix (2026-09): "compare both"/"compare these" is
                # ONE plural reference, not "this side vs. some other side".
                # A relationship handler wants two independently-resolved
                # sides, so a plural primary was reported as "I can see one
                # side of that" - the comparison the shopper asked for over
                # exactly the products they pointed at is what `compare`
                # already does (N-ary, grounded).
                capability = Capability.COMPARE
                capability_confirmed_reference = True
                routing_claim_applied = True
            elif verified_interpretation.question_type in (
                "relationship", "explanation", "recommendation",
                "compound_comparison", "alternatives", "attribute_refinement",
            ):
                capability = Capability.ANSWER_QUESTION
                capability_confirmed_reference = True
                routing_claim_applied = True
            elif verified_interpretation.question_type == "state_change" or (
                verified_interpretation.conversational_repair in ("cancellation", "correction", "pause")
                and not claim_has_real_operation
                # Compound-turn fix (2026-09): a correction/cancellation/
                # pause tag on THIS claim does not by itself mean the whole
                # turn is a bare state change - the SAME message may ALSO
                # ask for a real, separate operation ("Actually, I don't
                # want lightweight anymore. Show me waterproof trail shoes
                # under 10000...") that the SAME claim's own `capability`
                # field already names correctly. Forcing ANSWER_QUESTION
                # unconditionally here silently discarded that real
                # operation - a live-observed compound-turn regression, not
                # the narrow "repair signal caught what question_type
                # missed" case this branch was written for (see below).
                # Only forces the state-change path when the claim's own
                # capability is NOT already a genuine, actionable one - the
                # exact "No, forget that" shape this branch exists for
                # (nothing left to search/act on once the correction is
                # applied) still routes here correctly, because that
                # claim's own capability is realistically 'conversational'/
                # 'unknown', never 'discover'/'cart_action'/etc.
            ):
                # Stabilization fix (2026-09): `conversational_repair` is a
                # SEPARATE field from `question_type` on the SAME claim,
                # set independently by the SAME model call - measured MORE
                # reliable for this exact judgment than `question_type ==
                # "state_change"` alone (live-reproduced: "No, forget
                # that." tagged `conversational_repair="cancellation"`
                # correctly while `question_type` came back "retrieve" on
                # the same call, so the state-change handling in
                # `node_answer_question` never ran at all). Trusting
                # EITHER signal here means a cancellation/correction/pause
                # reaches the one real handler for it whenever the model
                # recognized the shape at all AND named no other real
                # operation, not only when it recognized it in one
                # specific field.
                capability = Capability.ANSWER_QUESTION
                capability_confirmed_reference = True
                routing_claim_applied = True
                verified_interpretation.question_type = "state_change"
            else:
                try:
                    claimed_capability = Capability(verified_interpretation.capability)
                except ValueError:
                    claimed_capability = None
                if claimed_capability is not None and claimed_capability != Capability.UNKNOWN:
                    capability = claimed_capability
                    capability_confirmed_reference = True
                    routing_claim_applied = True
        # Stabilization fix (2026-09): the "model unavailable this
        # turn" preemptive attribute_refinement override that used
        # to live here (trusting `delta.priorities_add`/
        # `nice_to_have_add` alone) is retired - that decision now
        # happens ONLY at `node_discovery`, AFTER a real, literal
        # search has actually failed, using the SAME real,
        # already-merged `state.priorities`/`nice_to_have` as its
        # one deterministic signal. Removing the preemptive guess
        # here means a plain literal search is always tried FIRST;
        # it is never skipped in favor of a guess that this must be
        # a refinement.

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
        # Live-reproduced gap: "add both shoes and socks to my cart"
        # contains "both" (a real `looks_like_group_or_ordinal_reference`
        # hit inside `looks_like_reference`), so this backstop used to
        # claim COMPARE for it before `resolve_cart_target` - the one
        # authoritative cart-intent check (capability.py's own docstring)
        # - ever got a chance to decide otherwise; when that separate LLM
        # call declined this exact multi-category phrasing (its own,
        # independent variance), capability was already irreversibly
        # COMPARE, producing a dead-end "I don't have two products in view
        # to compare yet" reply to an unambiguous cart-add command. Gated
        # on `looks_like_explicit_cart_command` so an explicit "add"/"buy"/
        # "purchase" message never lets this backstop steal the turn -
        # `edge_after_route_capability`'s cart-check still runs first
        # regardless (unchanged), and this only controls what capability
        # is left AFTER that check declines: DISCOVER (the primary
        # model's own SOFT default, a benign literal search) instead of a
        # wrongly-confident COMPARE.
        # Deterministic preference-status backstop - see capability.py's
        # own docstring on the gap this closes ("What is my preference
        # now?" fell through to a literal catalog search for the sentence
        # itself, since no `question_type` names this shape). Checked
        # BEFORE `routing_claim_applied` - a message this unambiguous
        # about asking for the shopper's OWN stated state must never lose
        # to an unrelated, unconfirmed capability guess the primary model
        # happened to also produce this turn.
        if looks_like_preference_status_request(state["message"]) and capability in SOFT_CAPABILITIES:
            capability = Capability.ANSWER_QUESTION
            capability_confirmed_reference = True
        elif looks_like_store_discovery(state["message"]) and capability in SOFT_CAPABILITIES:
            # "What do you have?" asks about the STORE; whatever the model's unconfirmed guess (often
            # conversational), it is answered by node_discovery's store overview (real categories as
            # choices), never by a literal catalogue search for those words.
            capability = Capability.DISCOVER
            capability_confirmed_reference = True
        elif routing_claim_applied:
            pass
        elif (
            capability in SOFT_CAPABILITIES
            and looks_like_reference(state["message"])
            and not looks_like_explicit_cart_command(state["message"])
            and not looks_like_resume_request(state["message"])
            and len(recent_relevant_product_ids(state["new_state"], minimum=1)) >= 1
        ):
            capability = Capability.COMPARE
            capability_confirmed_reference = True
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

        # Live-reproduced gap (long-conversation verification): COMPARE is
        # deliberately excluded from SOFT_CAPABILITIES (capability.py's own
        # docstring: "each has its own separate confirmation elsewhere"),
        # but the PRIMARY model's own raw COMPARE_PRODUCTS hint reaches
        # here completely unconfirmed regardless - `route()` maps it
        # straight through, no independent check at all. Genuinely
        # observed: "tell me what socks you have" (no comparison wording,
        # no ordinal, nothing genuinely comparative) mis-hinted as
        # COMPARE_PRODUCTS by the primary call, then answered with
        # `compare.py`'s own honest "I don't have two products to compare
        # yet" - technically not a fabrication, but a dead-end reply to a
        # plain discovery question, never even attempting to look up
        # socks. `run_compare_turn` cannot itself refuse this the way it
        # refuses to invent a resolution, because a completely empty
        # comparison-in-view state and a merely-SMALL one look identical
        # to it - the decision belongs here, before compare.py is ever
        # reached, using the exact same real-structure check the
        # confirmed backstop above already trusts. Downgrades to DISCOVER
        # (never invents a different guess) only when NEITHER the
        # confirmed backstop fired NOR there is enough already-tracked
        # structure for a real comparison to mean anything - an
        # unconfirmed hint with nothing to confirm it against is not
        # trustworthy enough to dead-end a plain question.
        if (
            capability == Capability.COMPARE
            and not capability_confirmed_reference
            and len(recent_relevant_product_ids(state["new_state"], minimum=2)) < 2
        ):
            capability = Capability.DISCOVER
    # `verified_interpretation`'s own `question_type` may have been
    # overwritten just above ("state_change") - persisted back so
    # downstream nodes see the SAME refined value, never the raw one.
    return {
        "capability": capability,
        "hinted_product_id": state.get("understanding_hinted_product_id"),
        "hinted_product_ids": None,
        "hinted_quantity": None,
        "cart_operation": "add",
        "capability_confirmed_reference": capability_confirmed_reference,
        "verified_interpretation": verified_interpretation,
        "trace": state["trace"],
    }


def _plausible_cart_signal(message_intent: str | None, message: str = "") -> bool:
    """A cheap, zero-extra-cost pre-filter for whether it's worth spending
    the real `interpret_cart_reference` call this turn - reuses Stage 1's
    own `message_intent` classification (already computed on every turn,
    already trusted elsewhere in this package for a different purpose,
    `capability.py`'s own conversational routing) rather than a new
    heuristic. Fails SAFE: only skips the real check when Stage 1 has
    ALREADY classified this exact message as pure conversational chit-chat
    - a genuine cart command hiding inside a message the architecture
    already calls "conversational" would be a failure of a signal every
    other part of this codebase already depends on, not a new risk this
    filter introduces. Every message Stage 1 calls "shopping" still gets
    the real, narrow, bounded check.

    Live-reproduced gap (2026-09): "add both to my cart" - an unambiguous
    explicit cart command - was itself classified `message_intent=
    "conversational"` by the primary model on a real fraction of calls
    (genuine per-call sampling variance, the same class this session
    already found for other single-shot classifications), skipping the
    real cart check entirely and falling through to `node_conversational`'s
    own rhetorical synthesis ("I can add both... would you like to proceed
    with that?") - functionally the same "asks instead of acting, needs a
    second round-trip" complaint as a stray pending proposal, just with no
    proposal object behind it at all. `looks_like_explicit_cart_command`
    (the same closed-class check `node_route_capability`'s own COMPARE
    backstop now defers to) is checked here too so a real "add"/"buy"/
    "purchase" message can never be skipped by `message_intent` alone,
    whatever Stage 1's classification said."""
    from .capability import looks_like_explicit_cart_command

    if looks_like_explicit_cart_command(message):
        return True
    return message_intent != "conversational"


def edge_after_route_capability(state: ShopperTurnState) -> str:
    # Structural gap fix (2026-09): a CONFIRMED `ANSWER_QUESTION` decision
    # (a real, per-turn interpretation call already resolved this as a
    # relationship/explanation/recommendation/compound_comparison/
    # alternatives/attribute_refinement/state_change question) must never
    # be intercepted by the cart-signal prefilter below - live-reproduced:
    # "Actually I meant the shoes." (a correction with a pending relationship
    # question waiting on it) was classified `message_intent` as plausibly
    # shopping-shaped, so `_plausible_cart_signal` routed it into
    # `reference_resolution` before this function ever got to check
    # `capability_confirmed_reference` at all - `resolve_cart_target`'s own
    # separate, unrelated model call then produced an unrelated "ambiguous
    # cart" reply, and the real pending-question replay in
    # `node_answer_question` never got a turn. Only bypasses the cart check
    # when this turn's own confirmed interpretation did NOT also carry a
    # real cart operation - a message that is BOTH a correction AND an
    # explicit cart instruction still reaches the cart path exactly as
    # before, since `verified_interpretation.cart_operation` would be
    # non-"none" for that shape.
    verified_interpretation = state.get("verified_interpretation")
    if (
        state.get("friction") is None
        and state.get("capability_confirmed_reference")
        and verified_interpretation is not None
        and verified_interpretation.cart_operation == "none"
    ):
        return "capability_dispatch"
    # Structural priority fix: explicit commerce commands are checked
    # BEFORE mission/trade-off dispatch (see `_edge_after_fulfillment_check`
    # below, now only reached from here when this check declines). An
    # explicit friction routing decision is authoritative and must never
    # be silently overridden by cart-intent resolution misreading a
    # synthesized friction sentence - the second of the two real bugs this
    # session found in the old flat-guard shape.
    if (
        state.get("friction") is None
        and state.get("decision") is not None
        and state.get("gate") is not None
        and state.get("execution") is not None
        and _plausible_cart_signal(state.get("message_intent"), state["message"])
    ):
        return "reference_resolution"
    # Live-reproduced gap (long-conversation verification, same root cause
    # as the cart-priority fix above, applied consistently): `node_route_
    # capability` (just run) may have ALREADY promoted this turn to
    # COMPARE via its own CONFIRMED real-reference backstop
    # (`looks_like_reference`, genuine products to compare against, real
    # structure, not a guess). If a mission also happens to be active, the
    # mission gate below would otherwise absorb the turn anyway (rule 5's
    # own unconditional allowance for an already-active mission topic),
    # silently discarding `route_capability`'s own confirmed answer and
    # returning a generic item-tracking reply to a genuine analytical
    # question ("compare the featherline and the trailblazer").
    #
    # Deliberately gated on `capability_confirmed_reference`, NOT merely
    # `capability == COMPARE` - a second live-reproduced regression this
    # exact distinction closes: COMPARE (like DISCOVER/CONVERSATIONAL) is
    # also reachable from the PRIMARY model's own unconfirmed hint alone
    # (`route()`'s `hinted_action_type` mapping), with no independent
    # confirmation behind it - `capability.py`'s own `SOFT_CAPABILITIES`
    # docstring says as much. Before this session's mission-priority fix,
    # an active mission's own unconditional absorption (rule 5)
    # accidentally masked a wrong COMPARE hint for an ordinary message
    # ("tell me what socks you have" mis-hinted as COMPARE_PRODUCTS,
    # silently corrected back to mission tracking). Bypassing mission for
    # ANY capability==COMPARE outcome - confirmed or not - reintroduced
    # that exact live-reproduced bug in the opposite direction (a
    # plain socks question answered with "I don't have two products to
    # compare yet"). Only the CONFIRMED backstop path is trustworthy
    # enough to take terminal priority over an active mission; the
    # unconfirmed primary hint alone is not.
    if state.get("friction") is None and state.get("capability_confirmed_reference"):
        return "capability_dispatch"
    return _edge_after_fulfillment_check(state)


# `_names_an_add` (imported above from add_command.py): an add-style COMMAND - questions, advice requests, negations
# and conditionals ("should I add it?", "don't add it", "if it's cheap, add it") are not commands and never reach a
# cart. Never authorization by itself - the named product still has to be resolved and goes through fulfillment.


_WANTS_MORE = re.compile(r"\b(?:another|one more|more|again|second|extra|additional)\b", re.IGNORECASE)
_DEICTIC_ADD = re.compile(r"\b(?:this|that|it|these|those|them|one|both|all)\b", re.IGNORECASE)
_PLURAL_POINTER = re.compile(r"\b(?:these|those|them|both|all)\b", re.IGNORECASE)
_BARE_PLURAL = re.compile(r"\b(?:these|those|them)\b", re.IGNORECASE)
_EXPLICIT_COLLECTIVE = re.compile(r"\b(?:both|all|every|everything|each)\b", re.IGNORECASE)


async def _resolve_deictic_add(state: ShopperTurnState, trace: TurnTrace) -> dict:
    """Model-free handling of an add that points at something instead of naming
    it. Resolution is purely structural, against the products actually on
    screen (`current_view_product_ids`): one product in view and a singular
    pointer -> that product; a counted/collective pointer ("both", "the first
    two", "all three") -> exactly those positions; two in view and "these" ->
    both. Several candidates with no way to choose, or nothing in view -> a
    clarifying question. The result is a normal cart action (a proposal for
    the shopper to approve) or a question; this function never touches a cart."""
    message = state["message"]
    view_ids = current_view_product_ids(state["new_state"])
    n = len(view_ids)

    def _ask(text: str) -> dict:
        trace.cart_intent_checked = True
        trace.cart_intent_detected = True
        trace.capability = "cart_action_ambiguous"
        trace.response_used_model = False
        trace.finish()
        result = TurnResultV2(
            reply=text, state=state["new_state"],
            evidence=TurnEvidence(query=None, products=[], no_match_reason=None),
            used_model=False, trace=trace.as_dict(),
        )
        return {"trace": trace, "result": result}

    if n == 0:
        return _ask(
            "I don't have a product in view to add yet - ask me to find something "
            "first, or tell me exactly which one you want."
        )
    chosen: list[str] | None = None
    positions = resolve_group_selector(message, n) if n >= 2 else None
    # A bare "these"/"those"/"them" over a long list is NOT a count: it would
    # resolve to the whole list, which the shopper did not say. Only an explicit
    # collective ("both", "all of them", "all three") or a position does.
    if positions and n > 2 and len(positions) == n and _BARE_PLURAL.search(message)             and not _EXPLICIT_COLLECTIVE.search(message):
        positions = None
    if positions:
        chosen = [view_ids[i] for i in positions]
    elif n == 1:
        chosen = [view_ids[0]]
    elif n == 2 and _PLURAL_POINTER.search(message):
        chosen = list(view_ids)
    if chosen:
        trace.cart_intent_checked = True
        trace.cart_intent_detected = True
        return {
            "trace": trace,
            "capability": Capability.CART_ACTION,
            "cart_operation": "add",
            "hinted_product_ids": chosen,
            "hinted_quantity": None,
        }
    products = await _fetch_products(state["adapter"], view_ids[:8])
    options = "; ".join(f"{i + 1}. {p.title}" for i, p in enumerate(products))
    return _ask(
        f"Which one would you like me to add? {options}" if options
        else "Which one would you like me to add? Tell me its name."
    )


_GENERIC_ADD = re.compile(r"^\s*(?:please\s+)?(?:add|buy|purchase)\b|\b(?:add|buy|purchase)\b.{0,40}\b(?:to|in|into)\s+(?:my|the)\s+(?:cart|basket|bag)\b", re.IGNORECASE)
_BUDGET_WORDS = re.compile(r"\b(?:under|below|less than|within|up to|upto|over|above|budget)\b|\d{3,}", re.IGNORECASE)


async def _clarify_generic_add(state: ShopperTurnState, trace: TurnTrace) -> dict | None:
    """An explicit add ("add something blue", "buy running shoes") that named no
    product and pointed at nothing on screen. It is NOT answered by an ordinary
    search and never guessed: the shopper is asked which product they mean, with
    real catalogue matches for their words listed when there are any (and put in
    view, so "the second one" then works), or asked for the product's name when
    there are none. No proposal and no cart change happen before a product is
    identified."""
    message = state["message"]
    if _BUDGET_WORDS.search(message):
        return None  # a budget/constraint statement that happens to contain "add"
    words = [w for w in named_product._remainder(message).split() if w not in vocabulary._FILLER]
    candidates: list = []
    if words:
        try:
            result = await retrieve(state["adapter"], " ".join(words), limit=20)
        except Exception:  # noqa: BLE001 - a lookup failure just means we ask without options
            logger.warning("shopper_v2: generic-add lookup failed", exc_info=True)
            result = None
        if result is not None and not result.unavailable and result.products:
            score, best = best_matches_with_score(result.products, " ".join(words))
            if score > 0:
                # Only what can actually be bought is offered as an option.
                candidates = [p for p in best if _buyable(p)][:6]
    trace.cart_intent_checked = True
    trace.cart_intent_detected = True
    trace.capability = "cart_action_ambiguous"
    trace.response_used_model = False
    if candidates:
        options = "; ".join(f"{i + 1}. {p.title}" for i, p in enumerate(candidates))
        reply = f"Which one would you like me to add? {options}"
        await _record_product_set(
            session_id=state["session_id"], connection_id=state["connection_id"],
            new_state=state["new_state"], trace=trace, source_capability="cart_action_clarify",
            verified_ids=[p.product_id for p in candidates], target_topic_id=_active_topic_id(state),
        )
    else:
        reply = (
            "I'm not sure which product you mean - tell me its name, or ask me to show "
            "you some options first and I'll add the one you pick."
        )
    trace.finish()
    return {"trace": trace, "result": TurnResultV2(
        reply=reply, state=state["new_state"],
        evidence=TurnEvidence(query=None, products=[], no_match_reason=None),
        used_model=False, trace=trace.as_dict(), products=candidates,
    )}


async def node_reference_resolution(state: ShopperTurnState) -> dict:
    trace = state["trace"]

    # Rev. 2 architecture (2026-09), Phase 7, completed as the SOLE
    # authoritative cart-reference decision (migration, 2026-09): this is
    # the only place "which product does an add/remove/set_quantity/clear
    # target" is ever decided. `intent_resolution.py::resolve_cart_target`
    # below no longer makes that decision for "add" at all (its own
    # decision logic - the topic-blind `_resolve_group_reference` and the
    # active-topic-only ordinal/pronoun/name resolution - was deleted in
    # this migration); it is reached only to (a) classify remove/
    # set_quantity/clear against the real, current cart contents (a
    # legitimate, non-topic-scoped decision - a cart line is a single
    # source of truth, never ambiguous the way "which topic" is), or
    # (b) ask an honest clarifying question when this function's own
    # attempt above didn't resolve "add" cleanly. `verify()` itself now
    # also cross-checks the claimed scope against every OTHER real topic
    # in view (`verification.py::_cross_check_scope`) before trusting a
    # bare active/last-rendered default - the exact gap that let a
    # confidently-wrong topic guess through as "resolved cleanly."
    context: ConversationContext = state.get("conversation_context")
    # Production architecture (2026-09), Phase 1: tracks whether THIS
    # function already made its own real interpretation attempt for a cart
    # command - `resolve_cart_target`'s own separate `interpret_cart_
    # reference` call, further below, must not retry the identical
    # question when this one already failed (the "no cascading LLM
    # fallback" rule, production architecture review §3). Stays False
    # (permitting that call) whenever this function's own gate never even
    # fired - a message that isn't an explicit cart command reaches
    # `resolve_cart_target` as its genuinely first and only attempt.
    cart_interpretation_attempted = False

    # Naming a product the shop sells is enough to add it: "add Trailblazer
    # Running Shoe" resolves to THAT product whether or not it is in view and
    # whether or not it was searched for first (the model-free floor under
    # `verify()`; see named_product.py). Skipped when the model's own checked
    # resolution already picked a product, so the two never disagree.
    if _names_an_add(state["message"]) and state.get("decision") is not None:
        _vi = state.get("verified_interpretation")
        _model_resolved = bool(
            _vi is not None and _vi.cart_operation == "add"
            and _vi.resolved_reference is not None and _vi.resolved_reference.product_ids
            and not _vi.needs_clarification
        )
        # "Degraded": no usable model interpretation this turn (none was
        # attempted, or the attempt failed) - the deterministic rules below are
        # then the only reading of the message there is.
        _degraded = _vi is None or state["trace"].interpretation_used_model is False
        if not _model_resolved:
            _view_ids: set[str] = set()
            if context is not None:
                if context.active_topic:
                    _view_ids |= set(context.active_topic.product_ids)
                if context.last_rendered:
                    _view_ids |= set(context.last_rendered.product_ids)
                for _t in context.paused_topics:
                    _view_ids |= set(_t.product_ids)
            _view_products = await _fetch_products(state["adapter"], _view_ids) if _view_ids else []
            _named = await named_product.resolve_named_add(
                adapter=state["adapter"], message=state["message"],
                view_products=_view_products, allow_fuzzy=_degraded,
            )
            if _named is not None:
                trace.cart_intent_checked = True
                trace.cart_intent_detected = True
                return {
                    "trace": trace,
                    "capability": Capability.CART_ACTION,
                    "cart_operation": "add",
                    "hinted_product_ids": [_named.product_id],
                    "hinted_quantity": None,
                }
            elif _degraded and (
                _DEICTIC_ADD.search(state["message"])
                or looks_like_group_or_ordinal_reference(state["message"])
            ):
                # No interpretation available this turn ("I'll take this",
                # "add these"): resolve a pointer ONLY from what is on screen,
                # and only when that is unambiguous; otherwise ask. Never a
                # guess, never a cart change - an add is still only a proposal.
                return await _resolve_deictic_add(state, trace)
            elif _degraded and _GENERIC_ADD.search(state["message"]):
                _clarified = await _clarify_generic_add(state, trace)
                if _clarified is not None:
                    return _clarified

    if context is not None and looks_like_explicit_cart_command(state["message"]):
        # Production architecture (2026-09), natural-language cart-operation
        # fix: `node_understand` already ran the SAME turn's single
        # authoritative interpretation call, with a fetch pool now widened
        # to include real cart-line products (`_candidates_for_scope`'s new
        # "cart" branch) - reuse ITS result rather than calling
        # `interpret_turn` again here. That call used to run unconditionally
        # for every explicit-cart-verb message, a real second interpretation
        # authority for exactly these turns - the "no cascading LLM
        # fallback" rule already applied everywhere else. Only when nothing
        # was attempted yet this turn (`interpretation_attempted` False -
        # `context` itself was `None` at that point, e.g. the very first
        # turn of a session) does this function still make its own, single,
        # first attempt.
        already_attempted = bool(state.get("interpretation_attempted"))
        products: list = []
        verified: VerifiedInterpretation | None = None
        if already_attempted:
            cart_interpretation_attempted = True
            # Already a checked `VerifiedInterpretation` - node_understand
            # already called `verify()` this turn; never re-verify an
            # already-verified result.
            verified = state.get("verified_interpretation")
            products = state.get("interpretation_products") or []
        else:
            ids_to_fetch: set[str] = set()
            if context.active_topic:
                ids_to_fetch |= set(context.active_topic.product_ids)
            if context.last_rendered:
                ids_to_fetch |= set(context.last_rendered.product_ids)
            for t in context.paused_topics:
                ids_to_fetch |= set(t.product_ids)
            if context.cart_snapshot:
                ids_to_fetch |= {line.product_id for line in context.cart_snapshot.lines}
            if ids_to_fetch:
                cart_interpretation_attempted = True
                products = await _fetch_products(state["adapter"], ids_to_fetch)
                claim = await interpret_turn(
                    reasoning=state["reasoning"], message=state["message"], context=context,
                    active_topic_products=[
                        p for p in products
                        if context.active_topic and p.product_id in context.active_topic.product_ids
                    ],
                    last_rendered_products=[
                        p for p in products
                        if context.last_rendered and p.product_id in context.last_rendered.product_ids
                    ],
                    all_fetched_products=products,
                )
                if claim is not None:
                    verified = verify(claim, context, products, state["message"])
        if cart_interpretation_attempted:
            if verified is not None:
                # Natural-language cart-operation fix (2026-09): "clear" is
                # reference-less by design (`intent_resolution.py`'s own
                # docstring - "operation='clear' needs no reference at
                # all") - `resolved_reference` is correctly `None` for it
                # (verify() only builds one when `claim.reference_kind !=
                # "none"`, and a bare "clear my cart" has nothing to
                # reference). The general branch below required a non-empty
                # `resolved_reference.product_ids` unconditionally, which
                # structurally could never fire for "clear" - a real,
                # live-observed gap, not just the verb-gate above it.
                if verified.cart_operation == "clear" and not verified.needs_clarification:
                    trace.cart_intent_checked = True
                    trace.cart_intent_detected = True
                    return {
                        "trace": trace,
                        "capability": Capability.CART_ACTION,
                        "cart_operation": "clear",
                        "hinted_product_ids": None,
                        "hinted_quantity": None,
                    }
                if (
                    verified.cart_operation in ("add", "remove", "set_quantity")
                    and verified.resolved_reference is not None
                    and verified.resolved_reference.product_ids
                    and not verified.needs_clarification
                ):
                    trace.cart_intent_checked = True
                    trace.cart_intent_detected = True
                    return {
                        "trace": trace,
                        "capability": Capability.CART_ACTION,
                        "cart_operation": verified.cart_operation,
                        "hinted_product_ids": verified.resolved_reference.product_ids,
                        "hinted_quantity": None,
                    }
                # Naturalness fix (2026-09, live-reproduced): a genuine cart
                # intent that couldn't be picked DECISIVELY within its own
                # correctly-scoped candidates (e.g. "add the socks" with a
                # different topic active) must never fall through to
                # `resolve_cart_target`'s legacy clarify below - that
                # function only ever knows the ACTIVE topic
                # (`active_topic_product_ids`), so it would ask about
                # whatever's active, ignoring the very noun the shopper
                # used to name a DIFFERENT, correctly-identified topic.
                # `candidates_considered` is already the real, correctly-
                # scoped set `verify()` (including its own cross-topic
                # check) determined relevant - asking about THOSE is
                # honest and on-topic; asking about the active topic
                # instead would be a wrong-but-honest-SOUNDING question,
                # not genuinely honest.
                if (
                    verified.cart_operation in ("add", "remove", "set_quantity", "clear")
                    and verified.resolved_reference is not None
                    and verified.resolved_reference.candidates_considered
                ):
                    candidates = verified.resolved_reference.candidates_considered
                    verb = "add to your cart" if verified.cart_operation == "add" else "change in your cart"
                    if len(candidates) == 1:
                        clarify_text = (
                            f"I don't see that among what I can {verb} - did you mean "
                            f"{candidates[0].title}, or something else?"
                        )
                    else:
                        options = "; ".join(f"{i + 1}. {p.title}" for i, p in enumerate(candidates))
                        clarify_text = f"Which one did you mean? {options}"
                    trace.cart_intent_checked = True
                    trace.cart_intent_detected = True
                    trace.response_used_model = False
                    trace.finish()
                    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
                    result = TurnResultV2(
                        reply=clarify_text, state=state["new_state"], evidence=evidence,
                        used_model=False, trace=trace.as_dict(),
                    )
                    return {"trace": trace, "result": result}

            # Deterministic, no-model-call backstop: reached only when the
            # interpretation call itself was unavailable this turn (`claim
            # is None`) - the one case even the naturalness fix above can't
            # help, since there is no `verify()` result to read at all.
            # Real word-overlap (`best_matches_with_score` - the same
            # scorer `verification.py`'s own cross-check trusts) between
            # the message and EVERY real topic in view, never a guess: if
            # a topic OTHER than whatever's active scores higher, resolving
            # or asking against the active one alone would be exactly the
            # topic-blind mistake this migration closes, just via a
            # different trigger (model unavailability instead of a wrong
            # model guess). Only ever narrows to a real topic's own real
            # products - never invents one, never silently adds without an
            # exact single match.
            if verified is None:
                real_topics = (
                    ([context.active_topic] if context.active_topic else [])
                    + list(context.paused_topics)
                )
                scored = []
                for topic in real_topics:
                    topic_products = [
                        p for p in products if p.product_id in topic.product_ids
                    ]
                    if not topic_products:
                        continue
                    score, _ = best_matches_with_score(topic_products, state["message"])
                    scored.append((score, topic_products))
                scored.sort(key=lambda pair: -pair[0])
                if scored and scored[0][0] > 0:
                    candidates = scored[0][1]
                    verb = "add to your cart" if looks_like_explicit_cart_command(state["message"]) else "change in your cart"
                    if len(candidates) == 1:
                        clarify_text = (
                            f"I don't see that among what I can {verb} - did you mean "
                            f"{candidates[0].title}, or something else?"
                        )
                    else:
                        options = "; ".join(f"{i + 1}. {p.title}" for i, p in enumerate(candidates))
                        clarify_text = f"Which one did you mean? {options}"
                    trace.cart_intent_checked = True
                    trace.cart_intent_detected = True
                    trace.response_used_model = False
                    trace.finish()
                    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
                    result = TurnResultV2(
                        reply=clarify_text, state=state["new_state"], evidence=evidence,
                        used_model=False, trace=trace.as_dict(),
                    )
                    return {"trace": trace, "result": result}

    # FALLBACK: the existing, unchanged path - reached whenever the Rev. 2
    # attempt above didn't run at all (no context yet, no explicit cart
    # command), or when neither the model-based nor the deterministic
    # backstop above found any real topic that matched the message better
    # than nothing (never when either DID identify a real scope - see the
    # naturalness fixes just above).
    cart_target = await resolve_cart_target(
        reasoning=state["reasoning"],
        adapter=state["adapter"],
        message=state["message"],
        history=state["history"],
        state=state["new_state"],
        cart_id=state.get("cart_id"),
        skip_own_interpretation=cart_interpretation_attempted,
    )
    trace.cart_intent_checked = True
    trace.cart_intent_detected = cart_target.is_cart_intent
    if not cart_target.is_cart_intent:
        return {"trace": trace, "cart_interpretation_attempted": cart_interpretation_attempted}

    if cart_target.product_ids:
        return {
            "trace": trace,
            "capability": Capability.CART_ACTION,
            "cart_operation": cart_target.operation,
            "hinted_product_ids": cart_target.product_ids,
            "hinted_quantity": cart_target.quantity,
        }
    if cart_target.operation == "add" and cart_target.product_id:
        # Live-reproduced regression from moving this check ahead of
        # mission dispatch: a single "add X" naming an already-tracked
        # mission item (e.g. "add Energy Gel Box") used to be claimed by
        # mission handling FIRST, unconditionally, so it always reached
        # `missions.py`'s own `add_now` -> `fulfillment.start_fulfillment`
        # path (a direct add, variant question if needed). Now that this
        # check runs earlier, the SAME message resolves here first and
        # would otherwise fall into `node_cart_action`'s single-id branch
        # - the older `cart_proposal.py` pending-confirm flow ("I'd like
        # to add X... confirm to go ahead"), a real UX regression for
        # exactly the case this session's own `add_now` fix closed.
        # Deferring here - never resolving the cart intent at all for this
        # one case - lets the turn fall through to mission dispatch
        # exactly as it already correctly handles it. Multi-id "add both"
        # is NOT deferred: `node_cart_action`'s own 2+-id branch already
        # uses `fulfillment.start_fulfillment` directly, so there is
        # nothing to regress there regardless of mission state.
        mission_candidate_ids = {
            pid
            for topic in state["new_state"].focus_stack
            if topic.kind == "mission"
            for pid in topic.product_ids
        }
        if cart_target.product_id in mission_candidate_ids:
            trace.cart_intent_detected = False
            return {"trace": trace, "cart_interpretation_attempted": cart_interpretation_attempted}
    if cart_target.operation == "clear" or cart_target.product_id:
        return {
            "trace": trace,
            "capability": Capability.CART_ACTION,
            "cart_operation": cart_target.operation,
            "hinted_product_id": cart_target.product_id,
            "hinted_quantity": cart_target.quantity,
        }

    # (The Rev. 2 rescue attempt that used to live here has moved to the
    # TOP of this function, tried unconditionally before `resolve_cart_target`
    # is ever trusted - see that comment for why: the old path's own "all"
    # override could silently produce a WRONG answer, not just no answer,
    # so a rescue placed only here, after that wrong answer already won,
    # was too late to help.)

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
    # A DEFINITIVELY resolved cart action is terminal by graph structure -
    # capability_dispatch handles it directly; mission/tradeoff are never
    # reached for this turn at all, the literal version of "the model must
    # never get the opportunity to reinterpret an unambiguous commerce
    # command as a mission request." Anything else (not a cart intent this
    # turn) falls through to the SAME mission/tradeoff chain a turn would
    # have reached if the cart pre-filter had declined to check at all -
    # cart-checking and declining must never itself consume the turn.
    if state.get("capability") == Capability.CART_ACTION:
        return "capability_dispatch"
    # Structural gap fix (2026-09): this check only ever covered
    # CART_ACTION, so a CONFIRMED capability decision from
    # `node_route_capability`'s own real interpretation call (e.g.
    # `Capability.ANSWER_QUESTION` for a relationship/compound-comparison
    # question) had no protection here the way it already does at
    # `edge_after_route_capability`'s own equivalent check - live-
    # reproduced: "Compare the cheapest and the lightest" (containing
    # " and ", `_has_new_mission_shape`'s own trigger) fell through this
    # exact path into the mission heuristic and got hijacked into
    # "I'm now tracking: cheapest, lightest" instead of ever reaching the
    # question it was confirmed to be. A CONFIRMED capability must never
    # be second-guessed by the mission-shape heuristic, at either entry
    # point into this chain, not just one of them.
    if state.get("capability_confirmed_reference"):
        return "capability_dispatch"
    return _edge_after_fulfillment_check(state)


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
        Capability.ANSWER_QUESTION: "answer_question",
    }
    return mapping.get(capability, "unimplemented")


async def node_compare(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    context: ConversationContext = state.get("conversation_context")

    # Rev. 2 architecture (2026-09), Phase 4: the first real capability
    # cutover. Tried FIRST, only when the cheap closed-class prefilter
    # (`looks_like_reference` - never itself the decision, see
    # `capability.py`'s own demoted role for this check) suggests it's
    # worth the real interpretation call; `run_compare_turn` below remains
    # the complete, unweakened fallback whenever this doesn't resolve
    # cleanly to a real, unambiguous answer - nothing regresses, this only
    # ADDS coverage. Closes, structurally rather than by word-list patch:
    # novel superlative phrasing ("high price shoe", "costliest") measured
    # working in Phase 2's live tests, and the cross-topic scoping bug
    # measured closed in Phase 3's tests (a paused topic's own superlative
    # resolves within THAT topic, never the currently-active one).
    verified: VerifiedInterpretation | None = None
    products: list = []
    compare_resolved_ids: list[str] | None = None
    # Step 22 live finding (gpt-4o-mini, 2 of 4 runs): "compare the first two"
    # after returning to running shoes was answered from the model's
    # two-sided claim (second side = the paused socks topic), comparing a
    # shoe with socks. When the shopper's own words select a GROUP out of
    # what is in view and name no second side, the selection is made from
    # that view - by `run_compare_turn`'s structural resolver - and no
    # model-resolved reference, however it is scoped, may replace it.
    collective_over_view = (
        looks_like_collective_comparison(state["message"])
        and len(current_view_product_ids(state["new_state"])) >= 2
    )
    if context is not None and looks_like_reference(state["message"]) and not collective_over_view:
        reused = state.get("verified_interpretation")
        if reused is not None:
            # Production architecture (2026-09), Phase 1: reuse the ONE
            # real interpretation already computed this turn
            # (`node_route_capability`) instead of calling
            # `interpret_turn`/`verify()` again - this was one of six-plus
            # independent, unmemoized call sites for the identical turn
            # (production architecture review, Gap J). `interpretation_
            # products` carries the same real products that resolution was
            # verified against, so nothing here re-fetches either.
            verified = reused
            products = state.get("interpretation_products") or []
        elif not state.get("interpretation_attempted"):
            # No attempt has been made yet this turn at all (route_
            # capability's own, broader gate declined) - compare's own,
            # narrower gate may still legitimately make the FIRST attempt.
            # Never a second one: `interpretation_attempted` is only False
            # when nothing tried - see the "no cascading LLM fallback"
            # rule, production architecture review §3.
            ids_to_fetch: set[str] = set()
            if context.active_topic:
                ids_to_fetch |= set(context.active_topic.product_ids)
            if context.last_rendered:
                ids_to_fetch |= set(context.last_rendered.product_ids)
            for t in context.paused_topics:
                ids_to_fetch |= set(t.product_ids)
            if ids_to_fetch:
                products = await _fetch_products(state["adapter"], ids_to_fetch)
                claim = await interpret_turn(
                    reasoning=state["reasoning"], message=state["message"], context=context,
                    active_topic_products=[
                        p for p in products
                        if context.active_topic and p.product_id in context.active_topic.product_ids
                    ],
                    last_rendered_products=[
                        p for p in products
                        if context.last_rendered and p.product_id in context.last_rendered.product_ids
                    ],
                    all_fetched_products=products,
                )
                if claim is not None:
                    verified = verify(claim, context, products, state["message"])

    if verified is not None:
        # A plural reference the interpreter already resolved + verified
        # ("compare both", "the first two") combined with a comparison
        # intent is a request to COMPARE exactly that set - not a request
        # to be handed the selection back as a listing. The comparison
        # then runs over precisely those ids (`run_compare_turn`'s
        # `resolved_ids`), grounded and N-ary like any other comparison.
        _resolved_now = (
            list(verified.resolved_reference.product_ids)
            if verified.resolved_reference is not None else []
        )
        if (
            len(_resolved_now) >= 2
            and not verified.needs_clarification
            and (
                looks_like_comparison_request(state["message"])
                or verified.question_type in ("relationship", "compound_comparison")
                or verified.capability == "compare"
            )
        ):
            compare_resolved_ids = _resolved_now
        # Live-reproduced (2026-09): the model's own `capability`
        # tag varies between "compare" and "discover" for the
        # IDENTICAL, correctly-resolved reference (both are
        # reasonable labels for "which shoe was priciest" - the
        # tag is a classification label, the resolution is the
        # real, checkable claim). Gating on the tag alone
        # discarded a correct answer purely because of label
        # variance, in exactly the same intermittent way the OLD
        # closed-word-list routing did. The trustworthy signal is
        # `resolved_reference` itself: it only ever contains real
        # ids when `verify()` found an unambiguous match against
        # real, scope-correct data - accepted regardless of which
        # of these two labels the model happened to use, since
        # `node_compare` is only ever reached for a message that
        # already looks reference-shaped in the first place.
        if (
            verified.capability in ("compare", "discover")
            and verified.resolved_reference is not None
            and verified.resolved_reference.product_ids
            and not verified.needs_clarification
            and compare_resolved_ids is None
        ):
            resolved_products = [
                p for p in products if p.product_id in verified.resolved_reference.product_ids
            ]
            reply_text = _reply_for_verified_reference(verified, resolved_products)
            evidence = already_verified_evidence(
                resolved_products,
                {p.product_id: ["rev2_verified_reference"] for p in resolved_products},
            )
            trace.response_used_model = False
            trace.verified_count = len(resolved_products)
            await _record_event(
                connection_id=state["connection_id"], session_id=state["session_id"],
                shopper_id=state.get("shopper_id"),
                event_type=shopper_events.EventType.COMPARISON_STARTED,
                detail={"product_ids": [p.product_id for p in resolved_products]},
            )
            await _record_product_set(
                session_id=state["session_id"], connection_id=state["connection_id"],
                new_state=state["new_state"], trace=trace, source_capability="compare",
                verified_ids=[p.product_id for p in resolved_products],
                target_topic_id=_active_topic_id(state),
            )
            trace.finish()
            result = TurnResultV2(
                reply=reply_text, state=state["new_state"], evidence=evidence,
                used_model=False, trace=trace.as_dict(), products=resolved_products,
            )
            return {"trace": trace, "result": result}

        # Rev. 2 architecture (2026-09), Phase 5: bounded re-
        # interpretation (spec §5's ESCALATE step). Only when the
        # first pass found REAL candidates but couldn't pick
        # unambiguously among them (never when nothing resolved
        # at all - that's a genuinely different topic/reference,
        # not a narrowing problem an escalation could fix).
        # Exactly one round, per `REASONING_BUDGET` (currently 2:
        # this first interpret + one escalation) - never a loop.
        if (
            verified.resolved_reference is not None
            and verified.resolved_reference.ambiguous
            and verified.resolved_reference.candidates_considered
            and interpretation.REASONING_BUDGET >= 2
        ):
            narrowed = verified.resolved_reference.candidates_considered
            escalation_context = context.model_copy(
                update={
                    "last_rendered": LastRendered(
                        product_ids=[p.product_id for p in narrowed],
                        source_capability="escalation",
                    ),
                },
            )
            escalated_claim = await interpretation.escalate_ambiguous_reference(
                reasoning=state["reasoning"], message=state["message"],
                context=escalation_context, candidates=narrowed,
            )
            if escalated_claim is not None:
                escalated_verified = verify(escalated_claim, escalation_context, narrowed, state["message"])
                if (
                    escalated_verified.resolved_reference is not None
                    and escalated_verified.resolved_reference.product_ids
                    and not escalated_verified.resolved_reference.ambiguous
                ):
                    resolved_products = [
                        p for p in narrowed
                        if p.product_id in escalated_verified.resolved_reference.product_ids
                    ]
                    reply_text = _reply_for_verified_reference(escalated_verified, resolved_products)
                    evidence = already_verified_evidence(
                        resolved_products,
                        {p.product_id: ["rev2_verified_reference_escalated"] for p in resolved_products},
                    )
                    trace.response_used_model = False
                    trace.verified_count = len(resolved_products)
                    await _record_event(
                        connection_id=state["connection_id"], session_id=state["session_id"],
                        shopper_id=state.get("shopper_id"),
                        event_type=shopper_events.EventType.COMPARISON_STARTED,
                        detail={"product_ids": [p.product_id for p in resolved_products]},
                    )
                    await _record_product_set(
                        session_id=state["session_id"], connection_id=state["connection_id"],
                        new_state=state["new_state"], trace=trace, source_capability="compare",
                        verified_ids=[p.product_id for p in resolved_products],
                        target_topic_id=_active_topic_id(state),
                    )
                    trace.finish()
                    result = TurnResultV2(
                        reply=reply_text, state=state["new_state"], evidence=evidence,
                        used_model=False, trace=trace.as_dict(), products=resolved_products,
                    )
                    return {"trace": trace, "result": result}

    # FALLBACK: the existing, unchanged path - reached whenever the Rev. 2
    # path above didn't run (no context yet, no reference-shaped signal)
    # or didn't resolve cleanly (nothing verified, or genuinely ambiguous -
    # `run_compare_turn`'s own honest clarifying/dead-end replies still
    # apply exactly as before).
    reply_text, evidence, products_shown, used_model = await run_compare_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], message=state["message"],
        state=state["new_state"], resolved_ids=compare_resolved_ids,
    )
    trace.response_used_model = used_model
    trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
    await _record_event(
        connection_id=state["connection_id"], session_id=state["session_id"],
        shopper_id=state.get("shopper_id"),
        event_type=shopper_events.EventType.COMPARISON_STARTED,
        detail={"product_ids": [p.product_id for p in products_shown]},
    )
    await _record_product_set(
        session_id=state["session_id"], connection_id=state["connection_id"],
        new_state=state["new_state"], trace=trace, source_capability="compare",
        verified_ids=[p.product_id for p in products_shown],
        target_topic_id=_active_topic_id(state),
    )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence,
        used_model=used_model, trace=trace.as_dict(), products=products_shown,
    )
    return {"trace": trace, "result": result}


async def _resolve_anchor_ids(state: ShopperTurnState, *, min_count: int) -> list[str] | None:
    """Rev. 2 migration (2026-09): the SAME unified interpret+verify path
    every other migrated capability uses, reused here so compatibility/
    complementary never independently re-derive "which product" from
    `active_topic_product_ids` alone (their own prior structural gap -
    entirely blind to a paused topic, or to the shopper naming a specific
    product by name at all). Returns real, verified product ids from
    whatever scope `verify()` actually resolved - `None` when nothing
    resolves cleanly, telling the caller to fall through to its own
    existing, honest default (today's active-topic behavior, unchanged as
    a real fallback, never a second decision-maker)."""
    context: ConversationContext = state.get("conversation_context")
    if context is None or not looks_like_reference(state["message"]):
        return None

    # Production architecture (2026-09), Phase 1: reuse the ONE
    # interpretation already computed this turn (`node_route_capability`)
    # rather than calling `interpret_turn`/`verify()` again - and, per the
    # "no cascading LLM fallback" rule, never attempt a SECOND, independent
    # one when a real attempt already ran and failed this turn.
    reused = state.get("verified_interpretation")
    if reused is not None:
        verified = reused
        products = state.get("interpretation_products") or []
    elif state.get("interpretation_attempted"):
        return None
    else:
        ids_to_fetch: set[str] = set()
        if context.active_topic:
            ids_to_fetch |= set(context.active_topic.product_ids)
        if context.last_rendered:
            ids_to_fetch |= set(context.last_rendered.product_ids)
        for t in context.paused_topics:
            ids_to_fetch |= set(t.product_ids)
        if not ids_to_fetch:
            return None
        products = await _fetch_products(state["adapter"], ids_to_fetch)
        claim = await interpret_turn(
            reasoning=state["reasoning"], message=state["message"], context=context,
            active_topic_products=[
                p for p in products
                if context.active_topic and p.product_id in context.active_topic.product_ids
            ],
            last_rendered_products=[
                p for p in products
                if context.last_rendered and p.product_id in context.last_rendered.product_ids
            ],
            all_fetched_products=products,
        )
        if claim is None:
            return None
        verified = verify(claim, context, products, state["message"])
    if (
        verified.resolved_reference is not None
        and not verified.needs_clarification
        and len(verified.resolved_reference.product_ids) >= min_count
    ):
        return verified.resolved_reference.product_ids
    return None


async def node_compatibility(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    anchor_ids = await _resolve_anchor_ids(state, min_count=2)
    reply_text, evidence, products_shown, used_model = await run_compatibility_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], message=state["message"],
        state=state["new_state"], anchor_ids=anchor_ids,
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
    await _record_product_set(
        session_id=state["session_id"], connection_id=state["connection_id"],
        new_state=state["new_state"], trace=trace, source_capability="compatibility",
        verified_ids=[p.product_id for p in products_shown],
        target_topic_id=_active_topic_id(state),
    )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence,
        used_model=used_model, trace=trace.as_dict(), products=products_shown,
    )
    return {"trace": trace, "result": result}


async def node_complementary(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    anchor_ids = await _resolve_anchor_ids(state, min_count=1)
    reply_text, evidence, products_shown, used_model = await run_complementary_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], message=state["message"],
        state=state["new_state"], anchor_ids=anchor_ids,
    )
    trace.response_used_model = used_model
    trace.verified_count = sum(1 for e in evidence.products if e.relevance_verified)
    await _record_impressions(
        connection_id=state["connection_id"], session_id=state["session_id"],
        shopper_id=state.get("shopper_id"),
        product_ids=[p.product_id for p in products_shown],
    )
    await _record_product_set(
        session_id=state["session_id"], connection_id=state["connection_id"],
        new_state=state["new_state"], trace=trace, source_capability="complementary",
        verified_ids=[p.product_id for p in products_shown],
        target_topic_id=_active_topic_id(state),
    )
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=state["new_state"], evidence=evidence,
        used_model=used_model, trace=trace.as_dict(), products=products_shown,
    )
    return {"trace": trace, "result": result}


def _terminal_answer(trace: TurnTrace, new_state: ShoppingStateV2, reply: str, products: list[Product] | None = None) -> dict:
    evidence = TurnEvidence(query=None, products=[], no_match_reason=None)
    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=reply, state=new_state, evidence=evidence, used_model=False,
        trace=trace.as_dict(), products=products or [],
    )
    return {"trace": trace, "result": result}


def degraded_disclaimer(trace: TurnTrace) -> str | None:
    """Production architecture (2026-09), Phase 4: the ONE shared, honest
    degradation signal appended to a reply built from a fallback-shaped
    classification - closes Gap I generally (item 13's mandate: "an action
    request must never silently become discovery... uncertainty must
    result in appropriate clarification/degradation rather than fabricated
    certainty") rather than patched per reply site.

    Fires ONLY when a real interpretation attempt was made THIS turn and
    failed - never on an ordinary, fully-deterministic turn that never
    needed the model at all (cart_status/checkout/a plain browse with
    nothing to interpret), which would make this a spurious, alarming
    disclaimer on the common case rather than a genuine one. Two
    independent signals, either one sufficient: Stage 1's own extraction
    call (`understanding_used_model`) and `node_route_capability`'s own
    interpretation attempt (`interpretation_used_model`) - each can fail
    independently, and either failing means SOMETHING this turn needed the
    model for did not get a confident answer.

    Deliberately backend-only text, not a raw boolean surfaced to the
    frontend as a UI badge (production architecture review §3): a raw
    `used_model=False` fires on many ordinary, correctly-deterministic
    turns by design (e.g. cart_status), and would misfire as alarming
    there - only this function's own, narrower judgment is the honest
    signal to disclose."""
    if trace.understanding_used_model is False or trace.interpretation_used_model is False:
        return "(I couldn't fully process that message just now - if I missed something you said, feel free to repeat it.)"
    return None


def _preference_status_reply(new_state: ShoppingStateV2, trace: TurnTrace | None = None) -> str:
    """A genuine, honest echo of `new_state`'s own real, already-persisted
    fields - never a guess, never a model call: answers "what is my
    preference now?" from exactly the same fields ranking/cancellation
    already read and write. Degrades honestly (says so plainly) when
    nothing has been stated yet, matching this package's own zero-data
    discipline - never a confident-sounding empty answer.

    Production architecture (2026-09), Phase 4: when the persisted fields
    are empty AND this turn's own reasoning was degraded (`trace`, if
    given), the reply distinguishes "you genuinely told me nothing" from
    "you may have told me something, but I couldn't process it a moment
    ago" - the exact conflation Gap I named ("inability to process a
    preference must never become a confident 'you have no preference'")."""
    parts = []
    if new_state.must_have:
        parts.append("must-haves: " + ", ".join(new_state.must_have))
    if new_state.nice_to_have:
        parts.append("preferences: " + ", ".join(new_state.nice_to_have))
    if new_state.priorities:
        parts.append("priorities: " + ", ".join(new_state.priorities))
    if new_state.exclusions:
        parts.append("things to avoid: " + ", ".join(new_state.exclusions))
    if new_state.budget_amount is not None:
        currency = new_state.budget_currency or ""
        parts.append(f"budget: {new_state.budget_amount} {currency}".strip())
    if not parts:
        if trace is not None and degraded_disclaimer(trace) is not None:
            return (
                "I couldn't fully process your last message or two - if you told me a "
                "preference, could you repeat it? Otherwise, you haven't told me any yet."
            )
        return "You haven't told me any preferences yet this conversation."
    return "Here's what you've told me so far - " + "; ".join(parts) + "."


async def node_answer_question(state: ShopperTurnState) -> dict:
    """Rev. 2 architecture (2026-09) - `engine/shopper_v2/answer.py`'s own
    module docstring. The general handler for every `question_type` other
    than "retrieve": resolves what the question needs (already mostly done
    by `node_route_capability`'s own interpretation call, carried here as
    `verified_interpretation`), grounds it in real data, and answers the
    SHAPE of question actually asked - never a product listing standing in
    for an answer. Read-only except for `pending_question`/`active_products`
    bookkeeping - never touches the cart, Decision, or Risk Gate."""
    trace = state["trace"]
    new_state = state["new_state"]
    context: ConversationContext = state.get("conversation_context")
    verified: VerifiedInterpretation | None = state.get("verified_interpretation")

    if looks_like_preference_status_request(state["message"]):
        return _terminal_answer(trace, new_state, _preference_status_reply(new_state, trace))

    if verified is None:
        return _terminal_answer(
            trace, new_state,
            "I couldn't quite work out what you're asking - could you say it a different way?",
        )

    qtype = verified.question_type

    if qtype == "state_change":
        pending = new_state.pending_question
        if pending and verified.conversational_repair == "correction":
            ids_to_fetch: set[str] = set()
            if context is not None:
                if context.active_topic:
                    ids_to_fetch |= set(context.active_topic.product_ids)
                if context.last_rendered:
                    ids_to_fetch |= set(context.last_rendered.product_ids)
                for t in context.paused_topics:
                    ids_to_fetch |= set(t.product_ids)
            products = await _fetch_products(state["adapter"], ids_to_fetch) if ids_to_fetch else []
            resolved_side = pending.get("resolved_side")
            if resolved_side and pending.get("question_type") in ("relationship", "compound_comparison"):
                # Stabilization fix (2026-09): the known side (whichever
                # resolved correctly on the FIRST attempt) is never re-
                # asked for again - only the message the shopper actually
                # said THIS turn ("Actually I meant the shoes") is
                # interpreted, to find WHICH topic/scope it names. The
                # attribute to resolve the missing side BY is the one
                # already known from the first side (real, stored data),
                # applied deterministically (`resolve_missing_side`) -
                # never re-asking the model to restate an attribute a
                # correction like this one never repeats in the first
                # place. This replaces the old "combine both messages and
                # re-interpret everything from scratch" approach, which
                # required the model to get BOTH sides right again in one
                # shot and measurably sometimes lost the side that had
                # already resolved correctly.
                claim = await interpret_turn(
                    reasoning=state["reasoning"], message=state["message"], context=context,
                    active_topic_products=[
                        p for p in products
                        if context and context.active_topic and p.product_id in context.active_topic.product_ids
                    ],
                    last_rendered_products=[
                        p for p in products
                        if context and context.last_rendered and p.product_id in context.last_rendered.product_ids
                    ],
                    all_fetched_products=products,
                )
                await shopping_state_store.merge(
                    state["session_id"], state["connection_id"], clear_fields=["pending_question"],
                )
                products_by_id = {p.product_id: p for p in products}
                known_entity = answer.entity_from_stored_side(resolved_side, products_by_id)
                new_candidates: list[Product] = []
                if claim is not None:
                    scoped = verify(claim, context, products, state["message"])
                    if scoped.resolved_reference is not None:
                        new_candidates = scoped.resolved_reference.candidates_considered
                attribute = resolved_side.get("attribute")
                missing_entity = (
                    answer.resolve_missing_side(new_candidates, attribute)
                    if new_candidates and attribute else None
                )
                if known_entity is None or missing_entity is None:
                    return _terminal_answer(
                        trace, new_state,
                        "I still couldn't work that out - which other product or group did you mean?",
                    )
                currency = answer._currency_of(known_entity.products + missing_entity.products)
                reply_text = answer._relationship_reply(known_entity, missing_entity, currency)
                products_shown = known_entity.products + missing_entity.products
                trace.response_used_model = False
                trace.finish()
                result = TurnResultV2(
                    reply=reply_text, state=new_state,
                    evidence=answer._evidence_for(products_shown, "relationship_resolved_after_correction"),
                    used_model=False, trace=trace.as_dict(), products=products_shown,
                )
                return {"trace": trace, "result": result}

            # Fallback for pending question_types with no "resolved_side"
            # (explanation/recommendation/alternatives) - the original
            # combine-and-reinterpret approach, unchanged.
            combined_message = f"{state['message']}. {pending.get('original_message', '')}".strip()
            claim = await interpret_turn(
                reasoning=state["reasoning"], message=combined_message, context=context,
                active_topic_products=[
                    p for p in products
                    if context and context.active_topic and p.product_id in context.active_topic.product_ids
                ],
                last_rendered_products=[
                    p for p in products
                    if context and context.last_rendered and p.product_id in context.last_rendered.product_ids
                ],
                all_fetched_products=products,
            )
            await shopping_state_store.merge(
                state["session_id"], state["connection_id"], clear_fields=["pending_question"],
            )
            if claim is None:
                return _terminal_answer(
                    trace, new_state,
                    "I still couldn't work that out - could you name both things you'd like compared?",
                )
            verified = verify(claim, context, products, combined_message)
            qtype = verified.question_type
            if qtype in ("state_change", "retrieve"):
                qtype = pending.get("question_type", "retrieve")
        elif verified.conversational_repair in ("cancellation", "pause"):
            # Structural gap fix (2026-09): a cancellation must clear
            # WHATEVER recent state it plausibly refers to, not just ONE
            # of "a pending unresolved question" / "a just-stated
            # preference" as mutually-exclusive alternatives - live-
            # reproduced: a stale, still-unresolved `pending_question`
            # (from an EARLIER, unrelated comparison attempt) always won
            # this check first, so "no, forget that" right after "I'm
            # looking for something lightweight" cleared the wrong thing
            # (an old pending comparison) and left the actually-just-
            # stated "lightweight" priority untouched - it kept leaking
            # into later replies ("since lightweight matters to you...")
            # for the rest of the conversation. Both are real, independent
            # pieces of recent state; cancelling clears BOTH when both
            # exist, deterministically (see the priority-removal comment
            # below for why this never depends on the SEPARATE
            # `understand()` call's own `priorities_remove` naming
            # accuracy).
            # Production architecture (2026-09), Phase 3: a cancellation
            # must also withdraw a pending CartProposal (the older, still-
            # live mechanism for remove/set_quantity/clear/ambiguous-
            # mission-item flows) when one exists - previously reachable
            # only via a separate REST button (`ProposalsPanel`), with NO
            # conversational path to it at all (production architecture
            # review, Gap H). Reuses the EXACT same `db.cart_proposals.
            # set_state(..., state="REJECTED")` the REST `/reject` route
            # already calls - not a second, parallel cancellation
            # mechanism, the same one, given a second way to reach it.
            rejected_proposal = False
            cart_id = state.get("cart_id")
            if verified.conversational_repair == "cancellation" and cart_id:
                try:
                    pending_proposals = await cart_proposals_store.pending_for_cart(
                        state["connection_id"], cart_id, session_id=state["session_id"],
                    )
                except Exception:  # noqa: BLE001 - degrade honestly, never fail the turn over this
                    logger.warning("cart_proposal cancellation lookup failed", exc_info=True)
                    pending_proposals = []
                for proposal in pending_proposals:
                    try:
                        await cart_proposals_store.set_state(
                            state["connection_id"], proposal["proposal_id"], state="REJECTED",
                        )
                        rejected_proposal = True
                    except Exception:  # noqa: BLE001 - degrade honestly, never fail the turn over this
                        logger.warning("cart_proposal cancellation failed for %r", proposal.get("proposal_id"), exc_info=True)
            if pending:
                await shopping_state_store.merge(
                    state["session_id"], state["connection_id"], clear_fields=["pending_question"],
                )
            if verified.conversational_repair == "cancellation" and (new_state.priorities or new_state.nice_to_have):
                removed = new_state.priorities[-1] if new_state.priorities else new_state.nice_to_have[-1]
                # Production architecture (2026-09), Phase 2: an explicit
                # cancellation writes a tombstone for the removed value, not
                # just a list removal - see `cancelled_preferences`'s own
                # docstring on why a bare removal cannot by itself prevent
                # the identical value being silently re-derived later from
                # replayed history.
                if new_state.priorities and removed in new_state.priorities:
                    await shopping_state_store.merge(
                        state["session_id"], state["connection_id"], priorities_remove=[removed],
                        cancelled_preferences_add=[removed],
                    )
                else:
                    await shopping_state_store.merge(
                        state["session_id"], state["connection_id"], nice_to_have_remove=[removed],
                        cancelled_preferences_add=[removed],
                    )
            if rejected_proposal:
                reply = "No problem - I won't add that."
            elif verified.conversational_repair == "cancellation":
                reply = "No problem - let me know if you'd like to try something else."
            else:
                reply = "Sure, take your time."
            return _terminal_answer(trace, new_state, reply)
        else:
            return _terminal_answer(trace, new_state, "Got it.")

    if qtype in ("relationship", "compound_comparison"):
        reply_text, evidence, products_shown, pending_payload = await run_relationship_or_compound(
            verified=verified, message=state["message"],
            reasoning=state["reasoning"], state=new_state,
        )
        # Stabilization fix (2026-09): a bare "which is cheaper?" with no
        # explicit product noun of its own can fail to resolve EITHER
        # side via the model's own claim (measured live) - before asking
        # the shopper and giving up, try the deterministic, no-model-call
        # backstop using the real topic structure directly (active vs.
        # most recently paused). Only engaged when the first attempt
        # found NOTHING usable for at least one side - never overrides a
        # real, resolved answer.
        _ref_done = verified.resolved_reference is not None and bool(verified.resolved_reference.product_ids)
        _sec_done = verified.resolved_secondary is not None and bool(verified.resolved_secondary.product_ids)
        # Root-cause fix (2026-09): the topic-recency fallback (active topic
        # vs. the most recently paused one) exists for a BARE "which is
        # cheaper?" where the interpreter resolved NEITHER side. When one
        # side DID resolve ("compare the first two" over a single shown
        # product), pairing it with whatever topic was paused last is not a
        # backstop - it is the cross-topic leak (a waterproof shoe compared
        # with socks). The honest answer there is to ask for the other side.
        if pending_payload is not None and context is not None and not (_ref_done or _sec_done):
            fetch_ids: set[str] = set()
            if context.active_topic:
                fetch_ids |= set(context.active_topic.product_ids)
            for t in context.paused_topics:
                fetch_ids |= set(t.product_ids)
            if fetch_ids:
                fallback_products = await _fetch_products(state["adapter"], fetch_ids)
                products_by_id = {p.product_id: p for p in fallback_products}
                fallback = answer.deterministic_relationship_fallback(context, products_by_id)
                if fallback is not None:
                    entity_a, entity_b = fallback
                    currency = answer._currency_of(entity_a.products + entity_b.products)
                    reply_text = answer._relationship_reply(entity_a, entity_b, currency)
                    products_shown = entity_a.products + entity_b.products
                    evidence = answer._evidence_for(products_shown, "relationship_deterministic_fallback")
                    pending_payload = None
        if pending_payload is not None:
            await shopping_state_store.merge(
                state["session_id"], state["connection_id"],
                pending_question={
                    "question_type": qtype, "original_message": state["message"], **pending_payload,
                },
            )
    elif qtype == "explanation":
        scope_products: list[Product] = []
        scope_label = "what's shown"
        if context is not None and context.last_rendered and context.last_rendered.product_ids:
            scope_products = await _fetch_products(state["adapter"], context.last_rendered.product_ids)
        elif context is not None and context.active_topic:
            scope_products = await _fetch_products(state["adapter"], context.active_topic.product_ids)
            scope_label = context.active_topic.label or scope_label
        reply_text, evidence, products_shown = run_explanation(
            message=state["message"], products=scope_products, state=new_state, scope_label=scope_label,
        )
    elif qtype == "recommendation":
        # Root-cause fix (2026-09): a recommendation operates over exactly
        # the candidate set the conversation has already established - a
        # reference the interpreter resolved this turn ("recommend one of
        # the first two"), else what is on screen right now
        # (`current_view_product_ids`: after a comparison that is the
        # compared pair, after a search that search's verified results) -
        # never only the active topic's own (possibly stale/empty) id
        # list, and never a fresh catalog search. Live-reproduced:
        # "compare both" then "recommend any one" answered "couldn't find
        # anything matching that search" because the recommendation only
        # looked at the active topic, which the comparison never updated.
        scope_label = "what you're looking at"
        if context is not None and context.active_topic and context.active_topic.label:
            scope_label = context.active_topic.label
        resolved_ids = (
            verified.resolved_reference.product_ids
            if verified.resolved_reference is not None and verified.resolved_reference.product_ids
            else []
        )
        rec_ids = resolved_ids or current_view_product_ids(new_state)
        scope_products = await _fetch_products(state["adapter"], rec_ids) if rec_ids else []
        reply_text, evidence, products_shown = run_recommendation(
            products=scope_products, state=new_state, budget_amount=new_state.budget_amount,
            scope_label=scope_label, message=state["message"],
        )
    elif qtype == "alternatives":
        scope_label = context.active_topic.label if context is not None and context.active_topic else "this"
        # "Already shown" is everything currently in view (the rendered
        # set AND the active topic), not only the active topic's own ids -
        # after a comparison/recommendation the view is a narrowed subset
        # of the topic, and an "alternative" must be neither of those.
        already_ids = set(current_view_product_ids(new_state)) | (
            set(context.active_topic.product_ids) if context is not None and context.active_topic else set()
        )
        wider = await retrieve(state["adapter"], scope_label, limit=40)
        all_products = wider.products if not wider.unavailable else []
        reply_text, evidence, products_shown = run_alternatives(
            all_topic_products=all_products, already_shown_ids=already_ids, scope_label=scope_label,
        )
    elif qtype == "attribute_refinement":
        same_turn_facts = [
            {"key": c.subject, "value": c.statement} for c in verified.durable_fact_candidates
        ]
        reply_text, evidence, products_shown, new_active_ids = await run_tradeoff_turn(
            adapter=state["adapter"], message=state["message"], state=new_state,
            memory_facts=[*(state.get("memory_facts") or []), *same_turn_facts],
        )
    else:
        return _terminal_answer(trace, new_state, "Could you tell me more about what you'd like to know?")

    # Production architecture (2026-09), Phase 0: every question shape this
    # node answers (relationship/compound_comparison, explanation,
    # recommendation, alternatives, attribute_refinement) resolves and
    # shows real products - persisted here, once, through the single
    # shared writer, tagged with the actual question shape, so a later
    # reference ("add it" after a recommendation, "why is IT better" after
    # an explanation) has a real record to resolve against. Before this,
    # only discovery/tradeoff ever recorded what was shown; every other
    # branch here left no trace at all once the reply was returned.
    await _record_product_set(
        session_id=state["session_id"], connection_id=state["connection_id"],
        new_state=new_state, trace=trace, source_capability=qtype,
        verified_ids=[p.product_id for p in products_shown],
        target_topic_id=_active_topic_id(state),
    )

    trace.response_used_model = False
    trace.finish()
    result = TurnResultV2(
        reply=reply_text, state=new_state, evidence=evidence, used_model=False,
        trace=trace.as_dict(), products=products_shown,
    )
    return {"trace": trace, "result": result}


@_requires_pipeline(Capability.CART_ACTION)
async def node_cart_action(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    hinted_product_ids = state.get("hinted_product_ids")
    hinted_product_id = state.get("hinted_product_id")
    # Shopper item 3: EVERY genuine "add" - one item or several - starts
    # the same conversational fulfillment queue: one real product/variant
    # question at a time, each answer added to the real cart directly via
    # a trusted tap, never a proposal sitting pending on a separate
    # "Accept"/"No thanks" confirm elsewhere in the UI (see `fulfillment.py`'s
    # own module docstring for the full argument, and invariant 4 - "a tap
    # is trusted; the model's guess is not" - which this already satisfies
    # for the SIZE tap the same way `PROPOSE_CART_CHANGE`'s later accept tap
    # does). Live-reproduced, user-reported: a single resolved item used to
    # fall through to `run_cart_change_proposal_turn` below, producing
    # exactly that separate-approval UX for the single-item case even
    # though the multi-item case (the `len(...) >= 2` branch this replaces)
    # had already been fixed the same way. Unified here so "add both shoes
    # and socks" and "add the shoes" are the same one code path, never two
    # divergent UX patterns for what is structurally the same shopper
    # action (an explicit, resolved add).
    add_ids = (
        hinted_product_ids if hinted_product_ids
        else [hinted_product_id] if hinted_product_id
        else None
    )
    # Rev. 2 architecture (2026-09), Phase 7: cart_action cutover - a
    # SECOND safety net, only reached when `node_reference_resolution`'s
    # own Rev. 2 attempt (tried first, unconditionally, in that node -
    # see its own comment) didn't already resolve `add_ids`. Gated on
    # `not add_ids` specifically so a turn already correctly resolved
    # upstream never pays for a second, redundant model call here.
    #
    # Production architecture (2026-09), Phase 1: additionally gated on
    # `not cart_interpretation_attempted` - `add_ids` being unset does NOT
    # by itself mean nothing was tried; `node_reference_resolution` may
    # have already made a real attempt this turn and genuinely failed to
    # resolve anything. Retrying the identical question here would be
    # exactly the cascading LLM fallback the production architecture
    # review's §3 rule forbids. This block is now reached ONLY when no
    # attempt was made yet at all this turn - its own, first attempt.
    context: ConversationContext = state.get("conversation_context")
    if (
        not add_ids
        and not state.get("cart_interpretation_attempted")
        and context is not None
        and looks_like_explicit_cart_command(state["message"])
    ):
        ids_to_fetch: set[str] = set()
        if context.active_topic:
            ids_to_fetch |= set(context.active_topic.product_ids)
        if context.last_rendered:
            ids_to_fetch |= set(context.last_rendered.product_ids)
        for t in context.paused_topics:
            ids_to_fetch |= set(t.product_ids)
        if ids_to_fetch:
            products = await _fetch_products(state["adapter"], ids_to_fetch)
            claim = await interpret_turn(
                reasoning=state["reasoning"], message=state["message"], context=context,
                active_topic_products=[
                    p for p in products
                    if context.active_topic and p.product_id in context.active_topic.product_ids
                ],
                last_rendered_products=[
                    p for p in products
                    if context.last_rendered and p.product_id in context.last_rendered.product_ids
                ],
                all_fetched_products=products,
            )
            if claim is not None:
                verified = verify(claim, context, products, state["message"])
                if (
                    verified.cart_operation == "add"
                    and verified.resolved_reference is not None
                    and verified.resolved_reference.product_ids
                    and not verified.needs_clarification
                ):
                    add_ids = verified.resolved_reference.product_ids
    # Contract (Step 17): an explicit request to add ONE item is a proposal the
    # shopper then approves (PROPOSE_CART_CHANGE -> shopper accept -> risk gate
    # -> execution); nothing is added from the sentence itself. A variant, when
    # the product has several, is chosen at approval time through the existing
    # accept flow. Only several items at once ("add both") go through the
    # fulfillment queue, which asks each product's variant question and then
    # also ends in per-item proposals (see `fulfillment._add_item`).
    # Policy (Step 25, replacing the Step 17 rule for this one case): when the shopper's OWN words are an explicit
    # command to add ("add it to the cart" - never "should I add it?", "don't add it", "sounds good"), the resolved
    # product(s) go through the fulfillment flow with execution: the variant question when one is needed, the real
    # cart's "already there" check, then the same proposal accepted in this turn through the shared accept core.
    # Anything else (a model-inferred intent) stays a single proposal the shopper approves.
    explicit_add = state.get("cart_operation") == "add" and is_explicit_add_command(state["message"])
    if explicit_add and not add_ids and hinted_product_id:
        add_ids = [hinted_product_id]
    if explicit_add and add_ids:
        # Policy (Step 25): the model's pick is a guess, never an authorization - it is checked against the shopper's own
        # words. `screen` is what is on display (positions and "both"/"all of them" refer to it); `in_view` is everything
        # "it" could mean (the screen plus the active topic).
        screen = list(context.last_rendered.product_ids) if context and context.last_rendered else []
        if not screen and context and context.active_topic:
            screen = list(context.active_topic.product_ids)
        in_view: list[str] = list(screen)
        if context and context.active_topic:
            in_view += [i for i in context.active_topic.product_ids if i not in in_view]
        try:
            _view_products = await _fetch_products(state["adapter"], set(in_view) | set(add_ids))
        except Exception:  # noqa: BLE001 - an unreadable catalogue means ask, never guess
            logger.warning("shopper_v2: could not read products before an explicit add", exc_info=True)
            _view_products = []
        _by_id = {p.product_id: p for p in _view_products}

        def _item(pid):
            prod = _by_id.get(pid)
            price = prod.price.amount if prod is not None and prod.price is not None else None
            return (pid, prod.title if prod is not None else "", price)

        # A pick that is not in view is accepted only if the catalogue, asked with the shopper's own words, names
        # exactly that product.
        _verified: set[str] = set()
        for _pid in add_ids:
            if _pid in _by_id and _pid not in in_view:
                try:
                    _hit = await named_product.resolve_named_add(
                        adapter=state["adapter"], message=state["message"], view_products=[], allow_fuzzy=False)
                except Exception:  # noqa: BLE001
                    _hit = None
                if _hit is not None and _hit.product_id == _pid:
                    _verified.add(_pid)
        # Every product the shopper could mean must be readable; a silently shrunken view would make "it" look unique.
        _readable = all(i in _by_id for i in add_ids) and all(i in _by_id for i in in_view)
        if not _readable or reference_is_ambiguous(
                state["message"], [_item(i) for i in add_ids], [_item(i) for i in screen],
                [_item(i) for i in in_view], frozenset(_verified)):
            options = [_by_id[i].title for i in (in_view or add_ids) if i in _by_id][:5]
            listing = (" " + ", ".join(options) + ".") if options else ""
            trace.response_used_model = False
            trace.finish()
            return {"trace": trace, "result": TurnResultV2(
                reply="Which one would you like me to add? Tell me the name and I'll add it." + listing,
                state=state["new_state"], evidence=TurnEvidence(query=None, products=[], no_match_reason=None),
                used_model=False, trace={**trace.as_dict(), "cart_changed": False, "add_clarification": True},
            )}
    if add_ids and len(add_ids) == 1 and state.get("cart_operation") == "add" and not explicit_add:
        state = {**state, "hinted_product_id": add_ids[0]}
        add_ids = None
    if add_ids and state.get("cart_operation") == "add":
        reply_text, added_ids = await fulfillment.start_fulfillment(
            adapter=state["adapter"], decision=state["decision"], gate=state["gate"],
            execution=state["execution"], connection_id=state["connection_id"],
            session_id=state["session_id"], cart_id=state.get("cart_id"),
            message=state["message"], state=state["new_state"],
            product_ids=add_ids, quantity=state.get("hinted_quantity"),
            explicit=explicit_add, allow_duplicate=bool(explicit_add and _WANTS_MORE.search(state["message"])),
            shopper_id=state.get("shopper_id"),
        )
        for product_id in added_ids:
            await _record_event(
                connection_id=state["connection_id"], session_id=state["session_id"],
                shopper_id=state.get("shopper_id"),
                event_type=shopper_events.EventType.ADD_TO_CART,
                product_id=product_id,
                detail={"source": "fulfillment_add"},
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
                "cart_changed": bool(added_ids),
                "fulfillment_pending": _fulfillment_pending_dict(state["new_state"]),
            },
        )
        return {"trace": trace, "result": result}

    # A repeated request for something the real cart already holds is answered,
    # not proposed again (approving it would silently make quantity two) - unless
    # the shopper plainly asked for more ("another", "one more", "again").
    if (
        state.get("cart_operation", "add") == "add" and state.get("hinted_product_id")
        and state.get("cart_id") and not _WANTS_MORE.search(state["message"])
    ):
        try:
            _cart = await state["adapter"].get_cart(state["cart_id"])
            _held = next((ln for ln in _cart.lines if ln.product_id == state["hinted_product_id"]), None)
        except Exception:  # noqa: BLE001 - an unreadable cart never blocks an honest proposal
            logger.warning("shopper_v2: cart read failed before proposing", exc_info=True)
            _held = None
        if _held is not None:
            trace.response_used_model = False
            trace.finish()
            return {"trace": trace, "result": TurnResultV2(
                reply=(
                    f"You already have {_held.title} in your cart (quantity {_held.quantity}). "
                    "Say \"add another\" if you'd like a second one."
                ),
                state=state["new_state"],
                evidence=TurnEvidence(query=None, products=[], no_match_reason=None),
                used_model=False, trace=trace.as_dict(),
            )}

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
        stated_budget=state["new_state"].budget_amount,
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


def compound_primary_query(delta, new_state) -> str | None:
    """The retrieval text for the PRIMARY half of a compound turn ("show me X
    ... and compare them with Y"), built from what the interpretation
    STRUCTURALLY stated - the goal and the new requirements - never from the
    message. The raw message also names Y, and a literal search of it returns Y
    as if it were a result for X (live: "...compare them with the Trailblazer"
    put the Trailblazer first for a waterproof search). Y is the secondary
    operation's business. None when nothing structured exists to search with,
    so the caller keeps its normal query."""
    goal = (getattr(delta, "goal", None) or getattr(new_state, "goal", None) or "").strip()
    extras = [*(getattr(delta, "constraints_add", None) or []), *(getattr(delta, "must_have_add", None) or [])]
    # Words the shopper explicitly REMOVED this turn ("I don't want lightweight
    # anymore") must not survive in an established goal that still carries them.
    removed = {
        w.lower()
        for phrase in [
            *(getattr(delta, "priorities_remove", None) or []),
            *(getattr(delta, "nice_to_have_remove", None) or []),
        ]
        for w in str(phrase).split()
    }
    seen: set[str] = set()
    words: list[str] = []
    for word in " ".join([goal, *extras]).split():
        key = word.lower()
        if key in removed and word in goal.split():
            continue
        if key not in seen:
            seen.add(key)
            words.append(word)
    return " ".join(words) or None


async def _restore_resumed_topic(state: ShopperTurnState) -> dict | None:
    """"Go back to the running shoes I saw earlier": when the Focus Stack just
    resumed a paused topic and the message adds nothing new to search for (a
    resume phrase, no new query, no new budget), show THAT topic's own products
    again - the same set, in the same order, re-fetched so price and stock are
    current - instead of running a fresh catalogue-wide search that returns a
    different list. Anything else (a new constraint, a new search term, the
    topic's products gone) falls back to ordinary discovery."""
    decision = state.get("topic_decision")
    delta = state["delta"]
    if (
        decision is None or decision.action != "resume"
        or not looks_like_resume_request(state["message"])
        or delta.search_query or delta.budget_amount is not None
    ):
        return None
    topic = next(
        (t for t in state["new_state"].focus_stack if t.topic_id == decision.topic_id), None,
    )
    if topic is None or topic.kind != "discovery" or not topic.product_ids:
        return None
    products = await _fetch_products(state["adapter"], list(topic.product_ids))
    if not products:
        return None
    label = (topic.label or "").strip()
    names = ", ".join(p.title for p in products)
    reply = (
        f"Back to {label.lower() if label else 'what we were looking at'}: {names}."
        if len(products) > 1 else f"Back to {products[0].title}."
    )
    state["trace"].response_used_model = False
    return {
        "trace": state["trace"], "evidence": already_verified_evidence(products),
        "products": products, "deterministic_reply": reply, "show_all_truncated": False,
    }


async def node_discovery(state: ShopperTurnState) -> dict:
    trace = state["trace"]
    delta = state["delta"]
    new_state = state["new_state"]
    restored = await _restore_resumed_topic(state)
    if restored is not None:
        return restored
    if looks_like_store_discovery(state["message"]):
        # A broad question about the shop's offering: answered from the merchant's real departments (each
        # verified to hold something buyable), as tappable choices. Read-only; no product set is recorded.
        overview = await run_store_discovery_turn(state["adapter"], state["connection_id"])
        trace.category_choices = overview.categories
        trace.store_discovery = overview.status
        trace.response_used_model = False
        return {
            "trace": trace, "evidence": TurnEvidence(query=None, products=[], no_match_reason=None),
            "products": [], "deterministic_reply": overview.reply, "show_all_truncated": False,
        }
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
    if getattr(state.get("verified_interpretation"), "secondary_operation", None) is not None:
        delta_query = delta.search_query or compound_primary_query(delta, new_state) or query_intent.core_query

    evidence, products, _intent = await run_discovery_turn(
        reasoning=state["reasoning"], adapter=state["adapter"], session_id=state["session_id"],
        connection_id=state["connection_id"], message=state["message"],
        delta_query=delta_query, constraint_only_turn=constraint_only_turn,
        shopping_state=new_state.as_prompt_dict(), memory_facts=state.get("memory_facts"),
        trace=trace,
        category_hints=list(getattr(state.get("verified_interpretation"), "catalog_categories", None) or []),
    )
    # Stabilization fix (2026-09): architectural replacement for the two
    # competing, independently-unreliable model signals
    # (`interpret().durable_fact_candidates`, `understand().priorities_add`)
    # this session's own prior attempts used to PRE-EMPTIVELY classify a
    # bare preference statement ("I'm looking for something lightweight")
    # as attribute_refinement, before ever trying a literal search.
    # Neither signal is a real, structural fact - both are the model
    # re-guessing the SAME semantic question in a different field, so
    # their failures are correlated on hard turns, not independent. This
    # replaces BOTH with a single, real, already-deterministic fact this
    # turn already has: a literal discovery search for the message
    # genuinely found no CONFIDENT match - `evidence.no_match_reason`,
    # the exact honest-decline trigger this scenario was producing before
    # this fix ("none of them are a confident match", "I couldn't find
    # anything"). Bug fix within this same fix: the first version of this
    # check used `not products` - wrong, because `products` here is the
    # RAW candidate list (e.g. a generic keyword-search hit), which can be
    # non-empty even when NONE of them passed relevance verification
    # (`evidence.no_match_reason == "no_relevant_match"`) - measured live,
    # this made the check silently never fire for exactly the
    # "found some results, but none of them are a confident match" shape
    # this fix targets, only for the rarer literal-zero-candidates case.
    # Only THEN - after a real failure, never as a pre-emptive guess -
    # check whether `new_state.priorities`/`nice_to_have` (already merged
    # by `apply_delta`, BEFORE this node ever runs, from THIS turn's own
    # `understand()` delta if it said anything) has real content to
    # re-rank an already-established topic by. This needs only ONE signal
    # (state that's already been merged, not a field to trust blindly),
    # checked at the one point a real fact (search found nothing
    # confident) already tells us a plain new-search reading isn't
    # panning out - never overriding a literal search that actually found
    # something, and never a phrase-specific rule (works for ANY stated
    # preference, ANY catalog, ANY wording that a real search fails on).
    verified_interpretation = state.get("verified_interpretation")
    # Same-turn durable-fact signal (2026-09 follow-up fix): the ONE
    # remaining structural signal (`new_state.priorities`/`nice_to_have`,
    # from `understand()`'s own delta) is itself not always populated the
    # SAME turn a preference is stated - measured live: "I'm looking for
    # something lightweight" can reach here with BOTH empty even though
    # `node_route_capability`'s own interpretation call (already run this
    # turn, unconditionally, whenever real context exists) DID reliably
    # recognize it as `durable_fact_candidates`. This is NOT a second
    # competing classifier deciding routing (the routing decision stays
    # exactly the one real fact - discovery found nothing confident) -
    # it is the SAME already-computed fact from this SAME turn, handed to
    # the deterministic re-rank as DATA, the same role `memory_facts`
    # already plays in `mission_ranking.rank_and_filter`'s own documented
    # precedence rule. Never invents a preference: only ever the model's
    # own already-extracted candidate for THIS turn's own message.
    same_turn_facts = (
        [
            {"key": c.subject, "value": c.statement}
            for c in verified_interpretation.durable_fact_candidates
        ]
        if verified_interpretation is not None else []
    )
    has_rerank_signal = bool(new_state.priorities or new_state.nice_to_have or same_turn_facts)
    if evidence.no_match_reason is not None and has_rerank_signal:
        ledger_ids = recent_relevant_product_ids(new_state, minimum=1)
        if ledger_ids:
            combined_memory_facts = [*(state.get("memory_facts") or []), *same_turn_facts]
            tt_reply, tt_evidence, tt_products, tt_new_active_ids = await run_tradeoff_turn(
                adapter=state["adapter"], message=state["message"], state=new_state,
                memory_facts=combined_memory_facts,
            )
            if tt_products:
                if tt_new_active_ids:
                    await shopping_state_store.merge(
                        state["session_id"], state["connection_id"], active_products=tt_new_active_ids,
                    )
                trace.response_used_model = False
                return {
                    "trace": trace, "evidence": tt_evidence, "products": tt_products,
                    "deterministic_reply": tt_reply, "show_all_truncated": False,
                }
    # Every downstream consumer (the reply text in `respond.py`, the
    # product cards in `finalize`, and `active_products`/
    # `recent_product_groups` state) reads the SAME resulting `products`
    # list, so a display/order decision made once here can never be
    # contradicted by a different one computed somewhere else downstream.
    evidence, products, deterministic_reply = _apply_query_intent(evidence, products, query_intent)
    # An honest, deterministic statement from domain grounding ("nothing is described
    # as fruity", "that one is sold out") replaces the generic listing sentence.
    domain_reply = (trace.query_normalization or {}).get("domain_reply")
    if domain_reply and not deterministic_reply:
        deterministic_reply = domain_reply
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


def _semantic_topic_label(products: list[Product], ids: list[str]) -> str:
    """A real, catalog-grounded topic label - never the shopper's raw
    sentence (see the grounding fix this replaces, at `node_topic_
    transition`'s own push site: a raw sentence used as a label was later
    reused verbatim as a literal catalog search string by `run_alternatives`,
    pulling in unrelated products). Built ONLY from real product titles
    already verified this turn - never invents a category name:

    - the distinctive words shared across the MOST of the given titles
      (majority vote - "Running Shoe" for a set of running shoes), title-
      cased, when at least two titles share a real word: or
    - the single product's own title, when there is only one; or
    - the first product's own title, as a last resort, when nothing is
      shared - still a real product name, never a guess.

    Empty only when `products`/`ids` themselves are empty - the caller
    never invokes this without real, already-verified products."""
    ordered = [p for p in products if p.product_id in set(ids)]
    if not ordered:
        return ""
    if len(ordered) == 1:
        return ordered[0].title
    counts: dict[str, int] = {}
    for p in ordered:
        for w in _distinctive_title_words(p.title):
            counts[w] = counts.get(w, 0) + 1
    if counts:
        # Bug fix (live-reproduced): restricting to words tied for the
        # SINGLE highest count collapsed a real, distinctive multi-word
        # category ("Running Shoe") down to whichever single generic noun
        # happened to appear in literally every title ("Shoe" alone,
        # since "Running" appeared in 6/9 titles - real and distinctive,
        # but never tied for the max of 9). Any word appearing in at
        # least two titles is real, shared signal - ordered by how many
        # titles share it (most first), so a genuinely common qualifier
        # ("Running") still ranks ahead of the generic head noun ("Shoe")
        # it modifies, not excluded by it.
        shared = {w for w, c in counts.items() if c >= 2}
        if shared:
            ordered_words = sorted(shared, key=lambda w: -counts[w])
            # Re-order the top few by first appearance among themselves,
            # so "Running Shoe" reads naturally rather than count-sorted
            # nonsense like "Shoe Running" for a tied case.
            top = set(ordered_words[:3])
            seen: list[str] = []
            for p in ordered:
                for w in re.findall(r"[a-z0-9]+", p.title.lower()):
                    if w in top and w not in seen:
                        seen.append(w)
            if seen:
                return " ".join(w.capitalize() for w in seen[:3])
    return ordered[0].title


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
        await _record_impressions(
            connection_id=connection_id, session_id=session_id, shopper_id=state.get("shopper_id"),
            product_ids=verified_ids,
        )

        # Focus Stack: the discovery topic decided for this turn
        # (`node_topic_transition`, already applied to the stack SHAPE -
        # push/resume/continue - before retrieval ran) gets its real
        # `product_ids` attached now that they're known, the same "shape
        # now, content when known" split `active_products` itself already
        # uses. Never a second, independently-derived list - the SAME
        # `verified_ids` just written above.
        decision = state.get("topic_decision")
        new_topic_id = state.get("new_topic_id")
        target_topic_id = (
            new_topic_id if new_topic_id
            else decision.topic_id if decision is not None and decision.action in ("continue", "resume")
            else None
        )
        # The deferred-push case: `node_topic_transition` deliberately
        # skipped pushing a topic when this turn looked mission-shaped
        # (see its own docstring) so mission handling could create a
        # `kind="mission"` topic instead if it claimed the turn. Reaching
        # `node_finalize` at all means mission declined (`handled=False`)
        # and this was, after all, an ordinary discovery push - perform it
        # now, never leaving a legitimate new topic uncreated just because
        # the mission-shape guess didn't pan out.
        if target_topic_id is None and decision is not None and decision.action == "push":
            new_topic_id = uuid.uuid4().hex[:12]
            new_stack = apply_transition(
                focus_stack=new_state.focus_stack, decision=decision,
                new_topic_id=new_topic_id, new_topic_kind="discovery",
                new_topic_label=_semantic_topic_label(products, verified_ids),
            )
            new_state.focus_stack = new_stack
            target_topic_id = new_topic_id
        # Foundation fix (2026-09): a PLAIN "show me X" turn - no
        # `delta.goal`, live-reproduced as unreliable for this exact
        # phrasing (see `decide_transition`'s own rule 4 and its
        # docstring) - left `decision.action == "continue"` with
        # `decision.topic_id is None` (nothing active to continue) and,
        # before this fix, created NO topic at all: `target_topic_id`
        # stayed `None` all the way through this function, so a shopper's
        # very first "Show me some running shoes" never became a real,
        # addressable Focus Stack topic - every later capability that
        # needs to resolve "which of the last few things we discussed"
        # (a relationship/compound-comparison, an explicit cart reference,
        # a resume) had nothing but the bounded, unlabeled legacy-ledger
        # reconstruction to fall back on. A turn that found real, verified
        # products and has nothing to attach them to is exactly a push,
        # deterministically - never a guess: gated on `verified_ids` being
        # real and non-empty, the same fact the deferred-push case above
        # already trusts.
        elif (
            target_topic_id is None
            and decision is not None
            and decision.action == "continue"
            and decision.topic_id is None
            and verified_ids
        ):
            new_topic_id = uuid.uuid4().hex[:12]
            new_stack = apply_transition(
                focus_stack=new_state.focus_stack,
                decision=TransitionResult("push", None, mission_call_allowed=False),
                new_topic_id=new_topic_id, new_topic_kind="discovery",
                new_topic_label=_semantic_topic_label(products, verified_ids),
            )
            new_state.focus_stack = new_stack
            target_topic_id = new_topic_id
        # Self-healing category-mismatch guard: `decide_transition`'s PUSH
        # rule (rule 4) is gated on `delta.goal`, a Stage-1 LLM extraction
        # that live-reproduced testing showed is NOT reliably populated for
        # a plain "show me X" browse turn (measured: 0/8 on a first-ever
        # "show me running shoes" turn in one run, and inconsistent turn-to-
        # turn in a longer conversation) - when it doesn't fire, "continue"
        # is returned even though the shopper actually switched category
        # ("show me socks" right after "show me running shoes"), and this
        # block would otherwise silently overwrite the ACTIVE topic's own
        # `product_ids` with the new, unrelated category - corrupting a
        # topic still labeled "running shoes" with sock ids, and losing the
        # shoes group entirely (it was never paused, so no later "the shoe
        # you showed me" can ever resume it). Caught here, deterministically,
        # against the one thing this node actually knows for certain that
        # `node_topic_transition` couldn't have known before retrieval ran:
        # the real, verified product titles this turn is about to attach.
        # Never a guess: a genuine refinement of the SAME topic (a budget/
        # priority narrowing that still returns the same category) always
        # shares a distinctive label word with the new titles and is never
        # affected; only a real, wordless category mismatch retroactively
        # becomes a push, exactly mirroring the mission-deferred-push
        # self-heal immediately above.
        if (
            target_topic_id is not None
            and decision is not None
            and decision.action == "continue"
        ):
            active_topic = next(
                (t for t in new_state.focus_stack if t.topic_id == target_topic_id), None,
            )
            if (
                active_topic is not None
                and active_topic.kind == "discovery"
                and active_topic.product_ids
                and verified_ids
            ):
                label_words = _distinctive_title_words(active_topic.label)
                title_words: set[str] = set()
                for p in products:
                    if p.product_id in verified_ids:
                        title_words |= _distinctive_title_words(p.title)
                if label_words and not (label_words & title_words):
                    retro_decision = TransitionResult("push", None, mission_call_allowed=False)
                    new_topic_id = uuid.uuid4().hex[:12]
                    new_stack = apply_transition(
                        focus_stack=new_state.focus_stack, decision=retro_decision,
                        new_topic_id=new_topic_id, new_topic_kind="discovery",
                        new_topic_label=_semantic_topic_label(products, verified_ids),
                    )
                    new_state.focus_stack = new_stack
                    target_topic_id = new_topic_id
        if target_topic_id is not None and verified_ids:
            # Structural gap fix (2026-09): this used to overwrite
            # UNCONDITIONALLY, including with an EMPTY list - a turn whose
            # own search found NOTHING ("What else do you have?" failing
            # honestly) silently erased the topic's real, already-known
            # products, destroying the active topic entirely. A turn that
            # found nothing NEW learned nothing about what's no longer
            # real - the topic's existing, real product_ids are left
            # untouched; only a turn that actually verified something
            # updates them. This was the root cause behind a cascading
            # failure several turns later: with the topic erased, the
            # NEXT turn's `ConversationContext.active_topic` was `None`,
            # so the entry gate for real per-turn interpretation
            # (`node_route_capability`'s own `has_real_context` check)
            # never fired at all, silently disabling every downstream fix
            # in this same conversation from that point on.
            for t in new_state.focus_stack:
                if t.topic_id == target_topic_id:
                    t.product_ids = verified_ids
                    # Grounding fix (2026-09): a topic created before any
                    # real product was known (the topic_transition-time
                    # push branch, or a session pre-dating this fix)
                    # starts with an empty label rather than the shopper's
                    # raw sentence - see that push site's own comment.
                    # Backfilled here with the first real, catalog-
                    # grounded label this topic ever gets real products
                    # to build one from. Never overwrites a label already
                    # set - a topic keeps its FIRST real label for its
                    # whole life, the same "turn-accurate, never silently
                    # reused" discipline the original per-turn label fix
                    # already established, just grounded in real product
                    # data instead of the raw sentence.
                    if not t.label:
                        t.label = _semantic_topic_label(products, verified_ids)
                    break
            await shopping_state_store.merge(
                session_id, connection_id,
                focus_stack=[t.model_dump(mode="json") for t in new_state.focus_stack],
            )

        # Rev. 2 architecture (2026-09), Phase 1: record exactly what was
        # rendered this turn, separate from Focus Stack topic membership -
        # see `state.py::LastRendered`'s own docstring. Written after
        # `target_topic_id` is final (including the retroactive-push
        # self-heal above) so `source_topic_id` is never stale.
        #
        # Structural gap fix (2026-09): this used to overwrite
        # UNCONDITIONALLY with `verified_ids`, including when EMPTY - a
        # turn whose own search found nothing confident ("What else do you
        # have?" failing honestly) silently wiped `last_rendered` down to
        # nothing. Given Focus Stack pushes are not reliably created for a
        # plain discovery turn in this project (an already-documented,
        # separate characteristic), `last_rendered` is in practice the
        # PRIMARY real-context signal `node_route_capability`'s own
        # `has_real_context` entry gate depends on - wiping it here meant
        # every subsequent turn's real per-turn interpretation silently
        # stopped being attempted at all for the rest of that
        # conversation, however many turns later a real preference was
        # stated. A turn that found nothing NEW keeps the PREVIOUS real
        # `last_rendered` untouched - it does not claim "nothing is
        # rendered now" just because this one search failed.
        # Production architecture (2026-09), Phase 0: the ledger
        # (`active_products`/`recent_product_groups`) and `LastRendered`
        # writes both now go through the single, shared
        # `_record_product_set` writer (§2 of the production architecture
        # review) rather than this node's own hand-rolled copy - called
        # here, after `target_topic_id` is final (including the
        # retroactive-push self-heal above), so `source_topic_id` is never
        # stale. `target_topic_id`'s own `product_ids`/label were already
        # set by the topic-content block above (with label-backfill, which
        # the shared writer deliberately does not do); passing it again
        # here is an idempotent re-write of the same value, not a second,
        # disagreeing source.
        await _record_product_set(
            session_id=session_id, connection_id=connection_id,
            new_state=new_state, trace=trace, source_capability="discovery",
            verified_ids=verified_ids, target_topic_id=target_topic_id,
        )

    shown = [p for p in products if p.product_id in set(verified_ids)]
    reply_text = state["reply_text"]

    # Production architecture (2026-09), Phase 5 (routing-gap fix): a
    # compound turn's secondary operation is now executed ONCE, universally,
    # in `run_turn_via_graph` after the graph completes - never here. This
    # node's own primary capability (discovery) is only ONE of several a
    # compound turn's PRIMARY half can resolve to; executing the secondary
    # operation only from this one node silently dropped it whenever the
    # primary half was classified as anything else (state_change,
    # answer_question, mission, ...) - a real, live-observed gap. See
    # `run_turn_via_graph`'s own comment for the fix.
    trace.finish()
    # Production architecture (2026-09), Phase 4: reaching `node_finalize`
    # (the discovery path) with `cart_interpretation_attempted` True means
    # an explicit cart command ("add X") was seen this turn, a real
    # interpretation attempt was made for it, and it still fell through to
    # an ordinary literal search - exactly the audit's Finding A ("an
    # explicit cart command silently becomes a search, with no
    # disclosure"). Disclosed here, once, rather than leaving a plain
    # discovery reply indistinguishable from a genuinely successful,
    # intentional search.
    # Also disclosed when this turn found nothing to show at all: a
    # "couldn't find anything matching that search" reply is only honest if
    # the message was actually understood. When interpretation failed (a
    # provider limit, an outage) the shopper's real request - "recommend
    # one", "compare those" - was never read, and the literal search of its
    # own words finding nothing says nothing about the store. Never fires
    # when products are shown, or when no interpretation was needed.
    # A dead search that arrived with its own term and still found nothing
    # keeps the original recovery behaviour: the merchant's real department
    # list (never invented), with no degradation noise - the shopper did not
    # type a sentence that could have been misread.
    dead_search_fallback = (
        not shown
        and state.get("friction") == friction_recovery.DEAD_SEARCH
        and bool(state.get("friction_query"))
    )
    if dead_search_fallback:
        reply_text = await friction_recovery.dead_search_reply(state["adapter"])
    # An honest "nothing matches" is only useful with the way forward: when
    # the message WAS understood (no degradation) and the store genuinely
    # has nothing for it, say what the store does carry (its real
    # categories) rather than leaving a dead end.
    # Whether the store sells this at all is decided from its own catalogue
    # vocabulary, not from the model - so a degraded (model-free) turn can say
    # "we don't carry that" with full confidence and no processing disclaimer.
    not_carried = getattr(evidence, "shop_carries_category", None) is False and not shown
    if (
        not shown
        and not dead_search_fallback
        and getattr(evidence, "no_match_reason", None) in ("no_candidates", "no_relevant_match")
        and (degraded_disclaimer(trace) is None or not_carried)
    ):
        _cats = vocabulary.cached_categories(state["connection_id"])
        if _cats:
            reply_text = f"{reply_text} (This store carries: {', '.join(_cats[:8])}.)"
    if not dead_search_fallback and not not_carried and (state.get("cart_interpretation_attempted") or not shown):
        disclaimer = degraded_disclaimer(trace)
        if disclaimer is not None:
            view_ids = current_view_product_ids(new_state)[:4] if not shown else []
            view_products = (
                await _fetch_products(state["adapter"], view_ids) if view_ids else []
            )
            if view_products:
                # The shopper has an established view and this message was
                # never understood: do not tell them "nothing matches your
                # search" (the literal search of words like "recommend one"
                # says nothing about the store) - say the message was not
                # processed and keep the view they already have.
                reply_text = (
                    "I couldn't process that just now - please try again in a "
                    "moment. We were looking at: "
                    + ", ".join(p.title for p in view_products) + "."
                )
            else:
                reply_text = f"{reply_text} {disclaimer}"
    result = TurnResultV2(
        reply=reply_text,
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
    g.add_node("topic_transition", node_topic_transition)
    g.add_node("fulfillment_continue", node_fulfillment_continue)
    g.add_node("guided_turn", node_guided_turn)
    g.add_node("typed_confirm", node_typed_confirm)
    g.add_node("cart_proposal_continue", node_cart_proposal_continue)
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
    g.add_node("answer_question", node_answer_question)
    g.add_node("respond", node_respond)
    g.add_node("finalize", node_finalize)

    g.set_entry_point("load_context")
    g.add_edge("load_context", "understand")
    g.add_edge("understand", "apply_delta")

    # New precedence, per the approved architecture:
    #   apply_delta -> topic_transition -> route_capability
    #     -> [cheap pre-filter] -> reference_resolution (explicit commerce
    #        check - terminal if a cart action definitively resolves,
    #        otherwise falls through to the SAME mission/tradeoff chain
    #        below, exactly as if the check had declined outright)
    #     -> mission (topic_transition-gated - see _edge_after_fulfillment_check)
    #     -> tradeoff
    #     -> capability_dispatch (using the `capability` route_capability
    #        already computed - never recomputed)
    # Fulfillment-pending answering still wins over all of the above,
    # unconditionally, exactly as before.
    g.add_conditional_edges(
        "apply_delta", edge_after_apply_delta,
        {
            "fulfillment_continue": "fulfillment_continue",
            "cart_proposal_continue": "cart_proposal_continue",
            "guided_turn": "guided_turn",
            "typed_confirm": "typed_confirm",
            "topic_transition": "topic_transition",
        },
    )
    g.add_edge("typed_confirm", END)
    g.add_conditional_edges(
        "guided_turn", edge_after_guided_turn,
        {END: END, "topic_transition": "topic_transition"},
    )
    g.add_conditional_edges(
        "fulfillment_continue", edge_after_fulfillment_continue,
        {END: END, "topic_transition": "topic_transition"},
    )
    g.add_conditional_edges(
        "cart_proposal_continue", edge_after_cart_proposal_continue,
        {END: END, "topic_transition": "topic_transition"},
    )
    g.add_edge("topic_transition", "route_capability")
    g.add_conditional_edges(
        "route_capability", edge_after_route_capability,
        {
            "reference_resolution": "reference_resolution",
            "mission": "mission",
            "tradeoff": "tradeoff",
            "capability_dispatch": "capability_dispatch_router",
        },
    )
    g.add_conditional_edges(
        "reference_resolution", edge_after_reference_resolution,
        {
            END: END,
            "capability_dispatch": "capability_dispatch_router",
            "mission": "mission",
            "tradeoff": "tradeoff",
        },
    )
    g.add_conditional_edges(
        "mission", edge_after_mission,
        {END: END, "tradeoff": "tradeoff", "capability_dispatch": "capability_dispatch_router"},
    )
    g.add_edge("tradeoff", END)

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
            "answer_question": "answer_question",
        },
    )

    for terminal in (
        "compare", "compatibility", "complementary", "cart_action", "checkout",
        "payment_recovery", "order_status", "cart_status", "cart_intelligence",
        "friction_recovery", "conversational", "unimplemented", "answer_question",
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
    friction_query: str | None = None,
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
    # The structured term only means anything for a dead search. When present
    # it REPLACES the synthesized "I searched for X and nothing came up"
    # sentence as the turn's message: that sentence is the storefront's
    # wording, not something the shopper typed, and discovery must search for
    # the term itself.
    friction_query = (friction_query or "").strip()[:200] or None
    if friction != friction_recovery.DEAD_SEARCH:
        friction_query = None
    if friction_query:
        message = friction_query
    initial: ShopperTurnState = {
        "reasoning": reasoning,
        "adapter": adapter,
        "session_id": session_id,
        "connection_id": connection_id,
        "message": message,
        "friction": friction,
        "friction_query": friction_query,
        "memory_facts": memory_facts,
        "cart_id": cart_id,
        "decision": decision,
        "gate": gate,
        "execution": execution,
        "owner_keys": owner_keys,
        "shopper_id": shopper_id,
        "selected_variant_id": selected_variant_id,
    }
    # NOTE: a turn-level lock around the whole graph invocation was tried
    # here and reverted - live-reproduced, it deadlocked (a 30s hang on
    # the very first turn of a brand-new session). LangGraph's `ainvoke`
    # evidently runs node coroutines under `asyncio.current_task()`
    # identities that differ from the task that entered this function, so
    # a task-identity-based reentrant lock held here saw each inner
    # `merge()` call as a DIFFERENT task and blocked forever waiting for
    # itself. A hang is a strictly worse failure mode than the narrower
    # race this was meant to close (a lost update from two genuinely
    # concurrent turns for the same session) - see PROGRESS.md for the
    # honest, disclosed residual risk and what a correct fix would need
    # (a lock keyed by something other than task identity, verified
    # against LangGraph's actual execution model before being trusted).
    final_state = await _COMPILED_GRAPH.ainvoke(initial)
    result: TurnResultV2 = final_state["result"]

    # Production architecture (2026-09), Phase 5 (routing-gap fix): a
    # compound turn's secondary operation, executed exactly ONCE, here,
    # regardless of which primary capability this turn's own single
    # interpretation call resolved to. Previously only executed inside
    # `node_finalize` (the discovery path), which silently dropped a
    # real, model-populated `secondary_operation` whenever the SAME
    # message's primary half was classified as `state_change`,
    # `answer_question`, `mission`, or anything else - a real,
    # live-observed gap (a message correcting a preference, searching, AND
    # asking for a comparison in one turn degraded to a bare "Got it."
    # with the comparison silently discarded). `run_turn_via_graph` is the
    # ONE place every capability path already returns through, so this is
    # the single choke point that makes the fix universal without a
    # branch-by-branch patch. Never a second interpretation call - reads
    # the SAME `verified_interpretation` `node_understand` already
    # produced this turn. Uses `result.products` (this turn's own real,
    # already-verified primary results) as the intra-turn handle; when the
    # primary path produced no products of its own (e.g. a pure
    # state_change/answer_question turn), falls back to whatever is
    # currently active in state, so "compare with the Trailblazer" still
    # has something real to compare against.
    verified_interpretation = final_state.get("verified_interpretation")
    secondary_op = getattr(verified_interpretation, "secondary_operation", None)
    if secondary_op is not None:
        primary_products = result.products
        if not primary_products:
            new_state = final_state.get("new_state")
            if new_state is not None:
                fallback_ids = recent_relevant_product_ids(new_state, minimum=1)
                if fallback_ids:
                    primary_products = await _fetch_products(adapter, fallback_ids)
        if primary_products:
            secondary_text = await _execute_secondary_operation(
                secondary_op=secondary_op, primary_products=primary_products,
                adapter=adapter, reasoning=reasoning, message=message,
                new_state=final_state.get("new_state"),
            )
            if secondary_text:
                result = _dataclass_replace(result, reply=f"{result.reply} {secondary_text}")

    # Guided mission: once the REAL cart holds a product for an open stage
    # (this turn's fulfillment add, or a tap/approval since the last turn),
    # confirm it and open the next stage - or close with the summary. Reads
    # the cart only; a turn that added nothing leaves the reply untouched.
    try:
        advanced = await guided_mission.advance_from_cart(
            adapter=adapter, session_id=session_id, connection_id=connection_id,
            cart_id=cart_id,
        )
    except Exception:  # noqa: BLE001 - never fail the turn over the follow-up text
        logger.warning("guided mission advance failed", exc_info=True)
        advanced = None
    if advanced is not None:
        text, next_products = advanced
        result = _dataclass_replace(
            result, reply=f"{result.reply} {text}".strip(),
            products=next_products or result.products,
        )
        _bb = guided_mission.BUDGET_TRACE.get()
        if _bb is not None and isinstance(result.trace, dict):
            result = _dataclass_replace(result, trace={**result.trace, "basket_budget": _bb})
        # The next stage's options are now what the shopper is looking at:
        # record them so "the first one" / "add the Cloudstep" resolve there.
        end_state = final_state.get("new_state")
        mission_topic = (
            next((t for t in end_state.focus_stack if t.kind == "mission"), None)
            if end_state is not None else None
        )
        if next_products and mission_topic is not None:
            await _record_product_set(
                session_id=session_id, connection_id=connection_id, new_state=end_state,
                trace=final_state["trace"], source_capability="mission",
                verified_ids=[p.product_id for p in next_products],
                target_topic_id=mission_topic.topic_id,
            )
    return result
