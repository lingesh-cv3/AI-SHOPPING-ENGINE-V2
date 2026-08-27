"""Engine HTTP API.

Runs on port 8000. Everything the consoles and the shopper widget call.

Two groups of routes:

- Engine routes (/api/...) expose the engine itself: what a connection can do,
  what its risk policy is, and what the pipeline decides for a given situation.
- Shop routes (/api/shop/...) proxy commerce operations through the adapter. The
  storefront could call the sample merchant directly. It deliberately does not -
  routing through here proves the adapter returns normalized data, and it is how
  a real storefront with our widget would work.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from engine import db, expiry
from engine import session as session_store
from engine.decision import operation_for
from engine.risk import RULE_ORDER, AutomationMode, RiskPolicy, explain_rules
from shared.models import (
    ACTION_RISK_PROPERTIES,
    ActionType,
    CommerceError,
    ProposedAction,
    risk_properties_for,
)

from .deps import DEV_MERCHANT_NAME, MERCHANT_NAMES, engine
from .schemas import (
    ActionInfo,
    ApprovalDecision,
    ConnectionSummary,
    PolicyUpdate,
    RejectionView,
    RuleView,
    SimulateRequest,
    SimulateResponse,
)

app = FastAPI(
    title="CV3 AI Shopping Assistant Engine",
    description="Commerce intelligence and action layer. Platform-independent.",
    version="0.1.0",
)

# The storefront and consoles run on Vite's dev server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

API = "/api"

logger = logging.getLogger(__name__)


@app.on_event("startup")
async def _startup() -> None:
    await db.create_schema()

    # Restore merchant settings. Without this every restart silently reset every
    # merchant to Cautious, so automation a merchant had switched on quietly went
    # away and everything began queuing again.
    stored = await db.load_policies()
    engine.policies.hydrate(
        [
            RiskPolicy(
                connection_id=row["connection_id"],
                mode=AutomationMode(row["mode"]),
                auto_allowed={ActionType(a) for a in row["auto_allowed"]},
                blocked={ActionType(a) for a in row["blocked"]},
                approval_timeout_minutes=row["approval_timeout_minutes"],
            )
            for row in stored
        ]
    )
    if stored:
        logger.info("restored %d merchant policies", len(stored))

    # Expire approvals nobody actions. Held on app.state so shutdown can cancel it;
    # a task nobody keeps a reference to can be garbage collected mid-run.
    app.state.expiry_task = asyncio.create_task(expiry.run_forever())


@app.on_event("shutdown")
async def _shutdown() -> None:
    task = getattr(app.state, "expiry_task", None)
    if task is not None:
        task.cancel()
    await engine.close()
    await db.dispose()


def _adapter(connection_id: str):
    adapter = engine.registry.adapter_for(connection_id)
    if adapter is None:
        raise HTTPException(404, f"unknown connection '{connection_id}'")
    return adapter


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "cv3-engine",
        "ai_reasoning": "active" if engine.reasoning.enabled else "rules only (no key)",
        "action_types": len(ACTION_RISK_PROPERTIES),
    }


@app.get(f"{API}/connections", response_model=list[ConnectionSummary])
async def list_connections() -> list[ConnectionSummary]:
    """Every merchant connection CV3 operates. The Operations Console's home."""
    out: list[ConnectionSummary] = []
    for connection_id in engine.registry.connection_ids():
        caps = await engine.registry.get(connection_id)
        policy = engine.policies.get(connection_id)
        if caps is None:
            continue
        out.append(
            ConnectionSummary(
                connection_id=connection_id,
                merchant_name=MERCHANT_NAMES.get(connection_id, connection_id),
                platform=caps.platform,
                mode=policy.mode,
                supported_count=sum(1 for c in caps.operations.values() if c.supported),
                unsupported=[str(op) for op in caps.unsupported()],
            )
        )
    return out


@app.get(f"{API}/connections/{{connection_id}}/capabilities")
async def get_capabilities(connection_id: str) -> dict:
    """What this connection's platform can actually do.

    The honest answer, straight from the adapter. Unsupported operations carry the
    reason, so the console can explain rather than just show a cross.
    """
    _adapter(connection_id)
    caps = await engine.registry.get(connection_id, force=True)
    if caps is None:
        raise HTTPException(404, "no capability declaration")
    return {
        "connection_id": caps.connection_id,
        "platform": caps.platform,
        "supports_webhooks": caps.supports_webhooks,
        "payment_recovery_methods": [str(m) for m in caps.payment_recovery_methods],
        "operations": [
            {
                "operation": str(op),
                "supported": cap.supported,
                "reason": cap.reason,
                "constraints": cap.constraints,
            }
            for op, cap in sorted(caps.operations.items(), key=lambda kv: str(kv[0]))
        ],
    }


@app.get(f"{API}/policy/rules", response_model=list[RuleView])
def get_rules() -> list[RuleView]:
    """The risk rules in evaluation order, first match wins.

    Served from the gate itself rather than duplicated in the frontend, so the
    console can never drift out of step with the code that actually decides.
    """
    explanations = dict(explain_rules())
    return [
        RuleView(order=i + 1, rule=rule, explanation=explanations.get(rule, ""))
        for i, rule in enumerate(RULE_ORDER)
    ]


@app.get(f"{API}/policy/actions", response_model=list[ActionInfo])
def get_actions() -> list[ActionInfo]:
    """Every action type and its fixed risk properties.

    can_ever_be_automatic lets the policy editor disable financial actions in the
    UI, rather than letting a merchant tick a box the gate will silently override.
    Better to explain up front than to surprise them later.
    """
    out: list[ActionInfo] = []
    for action_type in ActionType:
        props = risk_properties_for(action_type)
        out.append(
            ActionInfo(
                action_type=str(action_type),
                financial=props.financial,
                reversible=props.reversible,
                touches_customer_data=props.touches_customer_data,
                can_ever_be_automatic=(
                    not props.financial
                    and props.reversible
                    and not props.touches_customer_data
                ),
            )
        )
    return out


@app.get(f"{API}/policy/{{connection_id}}")
def get_policy(connection_id: str) -> dict:
    policy = engine.policies.get(connection_id)
    return {
        "connection_id": policy.connection_id,
        "mode": str(policy.mode),
        "auto_allowed": sorted(str(a) for a in policy.auto_allowed),
        "blocked": sorted(str(a) for a in policy.blocked),
        "approval_timeout_minutes": policy.approval_timeout_minutes,
    }


@app.put(f"{API}/policy/{{connection_id}}")
async def set_policy(connection_id: str, update: PolicyUpdate) -> dict:
    """Update a connection's risk settings, and persist them.

    Awaited rather than fired and forgotten. A merchant who changes a setting and
    gets a success response should be able to rely on it surviving a restart, and a
    write that quietly failed would be worse than an error they can act on.

    Financial actions in auto_allowed are accepted without complaint. That is
    deliberate - the gate overrides them anyway, and refusing here would hide the
    override rather than demonstrate it.
    """
    policy = RiskPolicy(
        connection_id=connection_id,
        mode=update.mode,
        auto_allowed=set(update.auto_allowed),
        blocked=set(update.blocked),
    )
    engine.policies.set(policy)

    try:
        await db.save_policy(
            connection_id,
            mode=str(policy.mode),
            auto_allowed=sorted(str(a) for a in policy.auto_allowed),
            blocked=sorted(str(a) for a in policy.blocked),
            approval_timeout_minutes=policy.approval_timeout_minutes,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not persist policy for %s", connection_id)
        raise HTTPException(
            500, "the setting was applied but could not be saved"
        ) from exc

    return get_policy(connection_id)


@app.post(f"{API}/simulate", response_model=SimulateResponse)
async def simulate(req: SimulateRequest) -> SimulateResponse:
    """Run one situation through Reasoning -> Decision -> Risk.

    The most useful endpoint for seeing the engine work: it returns what was
    diagnosed, what was proposed, what was filtered out and why, what was
    selected, and how the gate classified it.

    Supplying candidates bypasses the model entirely, which is how the console
    explores "what would happen if I changed this setting" without spending a
    model call on a question that has nothing to do with reasoning.
    """
    adapter = _adapter(req.connection_id)

    reasoning = None

    if req.candidates is not None:
        candidates = [
            ProposedAction(action_type=a, operation=operation_for(a))
            for a in req.candidates
        ]
    else:
        # Gather what the model is allowed to see. The catalog sample is what lets
        # it propose a search term that will actually match something - without it,
        # an alternative is a guess.
        products = None
        catalog_sample = None
        cart = None
        order = None
        try:
            if req.query:
                found = await adapter.search_products(req.query, limit=8)
                products = found.products
            browse = await adapter.search_products("", limit=20)
            catalog_sample = browse.products
            if req.cart_id:
                cart = await adapter.get_cart(req.cart_id)
            if req.order_id:
                order = await adapter.get_order(req.order_id)
        except CommerceError:
            # Context is best-effort. A model reasoning with less context is far
            # better than a shopper turn that fails because a lookup did.
            pass

        reasoning = await engine.reasoning.reason(
            friction=req.friction,
            query=req.query,
            products=products,
            cart=cart,
            order=order,
            catalog_sample=catalog_sample,
        )
        candidates = reasoning.actions

    trace = await engine.decision.decide(
        candidates, connection_id=req.connection_id, friction=req.friction
    )
    decision = engine.gate.classify(trace.selected)

    # The model wrote its reply around its own proposals, before the Decision
    # Engine filtered them. When everything it proposed was unavailable, that reply
    # promises the shopper something this platform cannot do - offering to split a
    # payment on a store with no payment-recovery endpoint, for instance.
    #
    # Rather than a second model call, the reply is replaced with a fixed honest
    # one. Fixed text cannot promise a capability we do not have, which is exactly
    # the property needed here.
    model_reply = reasoning.reply if reasoning else None
    if trace.escalated_because_empty:
        shopper_reply = (
            "I can't sort this one out myself. Someone from the shop will pick "
            "this up shortly."
        )
    else:
        shopper_reply = model_reply

    # Record the case. Deliberately after the answer is computed and deliberately
    # not allowed to fail the turn: the shopper's answer already exists, and losing
    # an audit row is bad but losing the answer is worse.
    case_id = None
    try:
        case_id = await db.record_case(
            connection_id=req.connection_id,
            friction=str(req.friction) if req.friction else None,
            query=req.query,
            cart_id=req.cart_id,
            order_id=req.order_id,
            session_id=req.session_id,
            reasoning={
                "used_model": bool(reasoning and reasoning.used_model),
                "model_name": reasoning.model_name if reasoning else None,
                "diagnosis": (
                    reasoning.diagnosis.cause
                    if reasoning and reasoning.diagnosis
                    else None
                ),
                "evidence": (
                    reasoning.diagnosis.evidence
                    if reasoning and reasoning.diagnosis
                    else []
                ),
                "fallback_reason": reasoning.fallback_reason if reasoning else None,
                "model_reply": model_reply,
                "shopper_reply": shopper_reply,
                "prompt_tokens": reasoning.prompt_tokens if reasoning else None,
                "completion_tokens": reasoning.completion_tokens if reasoning else None,
            },
            decision={
                               # Stored with parameters, not just names. Execution needs the
                # search term the model actually suggested - without it, executing
                # a SUGGEST_ALTERNATIVE re-runs the query that already failed,
                # which is worse than doing nothing.
                "proposed": [
                    {
                        "action_type": str(c.action_type),
                        "parameters": c.parameters,
                        "rationale": c.rationale,
                        "confidence": c.confidence,
                    }
                    for c in candidates
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
        logger.exception("could not record case; continuing")

    return SimulateResponse(
        case_id=case_id,
        friction=str(req.friction) if req.friction else None,
        proposed=[str(c.action_type) for c in candidates],
        rejected=[
            RejectionView(
                action_type=str(r.action_type), reason=r.reason, detail=r.detail
            )
            for r in trace.rejected
        ],
        selected_action=str(trace.selected.action.action_type),
        selection_reason=trace.selected.selection_reason,
        escalated_because_empty=trace.escalated_because_empty,
        risk_outcome=str(decision.outcome),
        risk_rule=decision.policy_rule,
        risk_reason=decision.reason,
        financial=decision.properties.financial,
        reversible=decision.properties.reversible,
        used_model=bool(reasoning and reasoning.used_model),
        model_name=reasoning.model_name if reasoning else None,
        diagnosis=(
            reasoning.diagnosis.cause if reasoning and reasoning.diagnosis else None
        ),
        evidence=(
            reasoning.diagnosis.evidence if reasoning and reasoning.diagnosis else []
        ),
        reply=model_reply,
        shopper_reply=shopper_reply,
        fallback_reason=reasoning.fallback_reason if reasoning else None,
        prompt_tokens=reasoning.prompt_tokens if reasoning else None,
        completion_tokens=reasoning.completion_tokens if reasoning else None,
    )


# ---------------------------------------------------------------------------
# Approval queue
# ---------------------------------------------------------------------------


@app.get(f"{API}/approvals/{{connection_id}}")
async def list_approvals(connection_id: str) -> dict:
    """What a person needs to decide, oldest first.

    Each entry carries the full reasoning that produced it - the diagnosis, the
    evidence, what the AI wanted to say and what the shopper was actually told. A
    queue that shows only an action name asks a person to approve something they
    cannot evaluate.
    """
    _adapter(connection_id)
    return {"approvals": await db.pending_approvals(connection_id)}


@app.post(f"{API}/approvals/{{connection_id}}/{{approval_id}}")
async def decide(connection_id: str, approval_id: str, body: ApprovalDecision) -> dict:
    """Approve or reject one pending action.

    Returns changed=False when the approval was already decided, rather than
    silently overwriting. Two operators acting at once should produce one decision
    and one execution.
    """
    _adapter(connection_id)
    result = await db.decide_approval(
        connection_id,
        approval_id,
        approved=body.approved,
        decided_by=body.decided_by,
        note=body.note,
    )
    if result is None:
        raise HTTPException(404, "no such approval on this connection")

    # An approval that changes nothing executes nothing. Without this check, two
    # operators clicking approve would produce one decision and two executions -
    # which is precisely the double-charge the idempotency key exists to prevent,
    # arrived at from the other direction.
    if not result.get("changed"):
        return {**result, "executed": None}

    if not body.approved:
        # A rejection is a decision the shopper is waiting on just as much as an
        # approval. Recording it and telling nobody leaves them on a page that will
        # never update, which is the same failure expiry was built to fix.
        #
        # The operator's note is deliberately not passed through. It is written for
        # the next person to read the case - "customer already paid by transfer" -
        # and is often about the shop's own processes rather than anything the
        # shopper should see.
        case = await db.get_case(connection_id, result["case_id"])
        if case is not None and case.session_id:
            try:
                await session_store.add_turn(
                    session_id=case.session_id,
                    connection_id=connection_id,
                    speaker="assistant",
                    text=(
                        "I checked with the shop and they're not able to do that "
                        "one, sorry. If you'd still like a hand, ask me and I'll "
                        "pass it on."
                    ),
                    case_id=case.case_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not tell the shopper about a rejection")

        try:
            await db.record_outcome(
                connection_id=connection_id,
                case_id=result["case_id"],
                resolved=False,
                final_state="REJECTED",
                required_human=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record a rejection outcome")

        return {**result, "executed": None}

    if not body.approved:
        # A rejection is a decision the shopper is waiting on just as much as an
        # approval. Recording it and telling nobody leaves them on a page that will
        # never update, which is the same failure expiry was built to fix.
        #
        # The operator's note is deliberately not passed through. It is written for
        # the next person to read the case - "customer already paid by transfer" -
        # and is often about the shop's own processes rather than anything the
        # shopper should see.
        case = await db.get_case(connection_id, result["case_id"])
        if case is not None and case.session_id:
            try:
                await session_store.add_turn(
                    session_id=case.session_id,
                    connection_id=connection_id,
                    speaker="assistant",
                    text=(
                        "I checked with the shop and they're not able to do that "
                        "one, sorry. If you'd still like a hand, ask me and I'll "
                        "pass it on."
                    ),
                    case_id=case.case_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not tell the shopper about a rejection")

        try:
            await db.record_outcome(
                connection_id=connection_id,
                case_id=result["case_id"],
                resolved=False,
                final_state="REJECTED",
                required_human=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record a rejection outcome")

        return {**result, "executed": None}

    if not body.approved:
        # A rejection is a decision the shopper is waiting on just as much as an
        # approval. Recording it and telling nobody leaves them on a page that will
        # never update, which is the same failure expiry was built to fix.
        #
        # The operator's note is deliberately not passed through. It is written for
        # the next person to read the case - "customer already paid by transfer" -
        # and is often about the shop's own processes rather than anything the
        # shopper should see.
        case = await db.get_case(connection_id, result["case_id"])
        if case is not None and case.session_id:
            try:
                await session_store.add_turn(
                    session_id=case.session_id,
                    connection_id=connection_id,
                    speaker="assistant",
                    text=(
                        "I checked with the shop and they're not able to do that "
                        "one, sorry. If you'd still like a hand, ask me and I'll "
                        "pass it on."
                    ),
                    case_id=case.case_id,
                )
            except Exception:  # noqa: BLE001
                logger.exception("could not tell the shopper about a rejection")

        try:
            await db.record_outcome(
                connection_id=connection_id,
                case_id=result["case_id"],
                resolved=False,
                final_state="REJECTED",
                required_human=True,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not record a rejection outcome")

        return {**result, "executed": None}

    executed = await engine.execution.execute_case(connection_id, result["case_id"])
    return {
        **result,
        "executed": {
            "succeeded": executed.succeeded,
            "summary": executed.summary,
            "action_type": executed.action_type,
            "payload": executed.payload,
            "error_code": executed.error_code,
            "latency_ms": executed.latency_ms,
            "final_state": executed.final_state,
        },
    }


@app.get(f"{API}/cases/{{connection_id}}")
async def list_cases(connection_id: str, limit: int = 30) -> dict:
    """Recent cases. The merchant console's activity view."""
    _adapter(connection_id)
    cases = await db.list_cases(connection_id, limit=limit)
    return {
        "cases": [
            {
                "case_id": c.case_id,
                "friction_type": c.friction_type,
                "state": c.state,
                "query": c.query,
                "order_id": c.order_id,
                "diagnosis": c.diagnosis,
                "used_model": c.used_model,
                "selected_action": c.selected_action,
                "risk_outcome": c.risk_outcome,
                "risk_rule": c.risk_rule,
                "financial": c.financial,
                "created_at": c.created_at.isoformat(),
            }
            for c in cases
        ]
    }


@app.get(f"{API}/stats/{{connection_id}}")
async def get_stats(connection_id: str) -> dict:
    """Headline counts for the console."""
    _adapter(connection_id)
    return await db.stats(connection_id)

@app.get(f"{API}/report/{{connection_id}}")
async def merchant_report(connection_id: str, days: int = 30) -> dict:
    """What the engine did for this merchant.

    Distinct from /stats, which counts work for the operations console. This is
    written for the shop owner: shoppers helped, problems solved, money recovered,
    and how much of it needed their time.
    """
    _adapter(connection_id)
    return await db.merchant_report(connection_id, days=days)


@app.post(f"{API}/admin/expire")
async def run_expiry() -> dict:
    """Run the expiry sweep now.

    The sweeper runs on a timer, which makes the behaviour hard to demonstrate
    without waiting out an approval timeout. This does the same work immediately.
    """
    return {"expired": await expiry.sweep_once()}


# ---------------------------------------------------------------------------
# Operations console: CV3's own view, across every merchant
# ---------------------------------------------------------------------------


def _operator_connections() -> list[str]:
    """Which merchants this operator can see.

    Every registered connection, for now. When authentication arrives this becomes
    the set a given operator is permitted to see, and nothing downstream changes -
    which is why the repository takes a list rather than querying everything.
    """
    return list(engine.registry.connection_ids())


@app.get(f"{API}/ops/queue")
async def ops_queue() -> dict:
    """Everything waiting on a person, across every merchant.

    The per-merchant queue made an operator covering several clients switch between
    them to find their work, so the oldest case on a quiet shop could sit unseen
    while they worked a busy one. This is the same data ordered by how long a
    shopper has been waiting, which is the order that matters.
    """
    rows = await db.pending_across(_operator_connections())
    for row in rows:
        row["merchant_name"] = MERCHANT_NAMES.get(
            row["connection_id"], row["connection_id"]
        )
    return {"approvals": rows}


@app.get(f"{API}/ops/history")
async def ops_history(limit: int = 40) -> dict:
    """What has already been decided, and what came of it."""
    rows = await db.decided_across(_operator_connections(), limit=limit)
    for row in rows:
        row["merchant_name"] = MERCHANT_NAMES.get(
            row["connection_id"], row["connection_id"]
        )
    return {"decisions": rows}


@app.get(f"{API}/ops/stats")
async def ops_overview() -> dict:
    """Workload across every merchant."""
    stats = await db.ops_stats(_operator_connections())
    stats["by_merchant"] = {
        MERCHANT_NAMES.get(cid, cid): count
        for cid, count in stats["by_merchant"].items()
    }
    return stats
