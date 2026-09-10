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
import inspect
import logging

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from engine import copilot, db, expiry
from engine import session as session_store
from engine.decision import operation_for
from engine.risk import RULE_ORDER, AutomationMode, RiskPolicy, explain_rules
from shared.models import (
    ACTION_RISK_PROPERTIES,
    ActionType,
    Availability,
    CommerceError,
    Operation,
    ProposedAction,
    risk_properties_for,
)

from .auth import any_key, belongs_to, merchant_scoped, operator
from .deps import DEV_MERCHANT_NAME, MERCHANT_NAMES, engine
from .schemas import (
    HandoverDone,
    ActionInfo,
    ApprovalDecision,
    ConnectionSummary,
    CopilotAnswer,
    CopilotQuestion,
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
    # Authorization is listed explicitly rather than relying on the wildcard.
    # A browser will not send a header the server has not said it accepts, and
    # some stacks treat "*" as excluding Authorization for exactly that reason.
    # The operator key travels in this header, so being specific is the difference
    # between the console working and a request that never leaves the browser.
    allow_headers=["Content-Type", "Authorization"],
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
                holdout_percent=row.get("holdout_percent", 0),
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


async def _capabilities_dict(connection_id: str) -> dict:
    """What this connection's platform can actually do.

    The honest answer, straight from the adapter. Unsupported operations carry the
    reason, so the console (and the copilot) can explain rather than just show a
    cross. Factored out of the route below so the copilot can reuse the identical
    shape rather than a second, drifting summary of the same data.
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


@app.get(f"{API}/connections/{{connection_id}}/capabilities")
async def get_capabilities(
    connection_id: str, _=Depends(merchant_scoped())
) -> dict:
    return await _capabilities_dict(connection_id)


def _rules_list() -> list[RuleView]:
    """The risk rules in evaluation order, first match wins.

    Served from the gate itself rather than duplicated in the frontend, so the
    console (and the copilot) can never drift out of step with the code that
    actually decides.
    """
    explanations = dict(explain_rules())
    return [
        RuleView(order=i + 1, rule=rule, explanation=explanations.get(rule, ""))
        for i, rule in enumerate(RULE_ORDER)
    ]


@app.get(f"{API}/policy/rules", response_model=list[RuleView])
def get_rules() -> list[RuleView]:
    return _rules_list()


def _actions_list() -> list[ActionInfo]:
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


@app.get(f"{API}/policy/actions", response_model=list[ActionInfo])
def get_actions() -> list[ActionInfo]:
    return _actions_list()


def _policy_dict(connection_id: str) -> dict:
    policy = engine.policies.get(connection_id)
    return {
        "connection_id": policy.connection_id,
        "mode": str(policy.mode),
        "auto_allowed": sorted(str(a) for a in policy.auto_allowed),
        "blocked": sorted(str(a) for a in policy.blocked),
        "approval_timeout_minutes": policy.approval_timeout_minutes,
        "holdout_percent": policy.holdout_percent,
    }


@app.get(f"{API}/policy/{{connection_id}}")
def get_policy(
    connection_id: str, _=Depends(merchant_scoped())
) -> dict:
    return _policy_dict(connection_id)


@app.put(f"{API}/policy/{{connection_id}}")
async def set_policy(
    connection_id: str,
    update: PolicyUpdate,
    _=Depends(merchant_scoped()),
) -> dict:
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
        holdout_percent=update.holdout_percent,
    )
    engine.policies.set(policy)

    try:
        await db.save_policy(
            connection_id,
            mode=str(policy.mode),
            auto_allowed=sorted(str(a) for a in policy.auto_allowed),
            blocked=sorted(str(a) for a in policy.blocked),
            approval_timeout_minutes=policy.approval_timeout_minutes,
            holdout_percent=policy.holdout_percent,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("could not persist policy for %s", connection_id)
        raise HTTPException(
            500, "the setting was applied but could not be saved"
        ) from exc

    return get_policy(connection_id)


@app.post(f"{API}/simulate", response_model=SimulateResponse)
async def simulate(
    req: SimulateRequest, key=Depends(any_key)
) -> SimulateResponse:
    """Run one situation through Reasoning -> Decision -> Risk.

    The most useful endpoint for seeing the engine work: it returns what was
    diagnosed, what was proposed, what was filtered out and why, what was
    selected, and how the gate classified it.

    Supplying candidates bypasses the model entirely, which is how the console
    explores "what would happen if I changed this setting" without spending a
    model call on a question that has nothing to do with reasoning.
    """
    belongs_to(key, req.connection_id)
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
async def list_approvals(
    connection_id: str, _=Depends(merchant_scoped())
) -> dict:
    """What a person needs to decide, oldest first.

    Each entry carries the full reasoning that produced it - the diagnosis, the
    evidence, what the AI wanted to say and what the shopper was actually told. A
    queue that shows only an action name asks a person to approve something they
    cannot evaluate.
    """
    _adapter(connection_id)
    return {"approvals": await db.pending_approvals(connection_id)}


@app.post(f"{API}/approvals/{{connection_id}}/{{approval_id}}")
async def decide(
    connection_id: str,
    approval_id: str,
    body: ApprovalDecision,
    _=Depends(merchant_scoped()),
) -> dict:
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

    # Out of time. The queue stops showing an approval past its deadline, but a
    # console drawn a minute earlier still has a live button on it, and pressing
    # it used to execute - which on this queue means taking a payment against a
    # decision that had already lapsed.
    #
    # Swept rather than closed out here, so an approval that runs out while an
    # operator is looking at it ends the same way as one that runs out while
    # nobody is: the shopper is told, and the sale we did not save is counted.
    if result.get("expired"):
        try:
            await expiry.sweep_once()
        except Exception:  # noqa: BLE001
            logger.exception("could not sweep an approval decided after expiry")
        return {
            **result,
            "executed": None,
            "reason": (
                "that approval had already expired, so nothing was run. The "
                "shopper has been told."
            ),
        }

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
async def list_cases(
    connection_id: str,
    limit: int = 30,
    _=Depends(merchant_scoped()),
) -> dict:
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
async def get_stats(
    connection_id: str, _=Depends(merchant_scoped())
) -> dict:
    """Headline counts for the console."""
    _adapter(connection_id)
    return await db.stats(connection_id)

@app.get(f"{API}/report/{{connection_id}}")
async def merchant_report(
    connection_id: str,
    days: int = 30,
    _=Depends(merchant_scoped()),
) -> dict:
    """What the engine did for this merchant.

    Distinct from /stats, which counts work for the operations console. This is
    written for the shop owner: shoppers helped, problems solved, money recovered,
    and how much of it needed their time.
    """
    _adapter(connection_id)
    report = await db.merchant_report(connection_id, days=days)
    caps = await engine.registry.get(connection_id)
    report["supports_payment_recovery"] = bool(
        caps and caps.payment_recovery_methods
    )
    # A short top-products list, from the same OrderLine data the copilot's
    # product-performance answers use - a small, coherent addition to the
    # existing report shape, not a second dashboard. Limited to 5 and to
    # quantity sold, the figure a merchant glances at first; the copilot
    # answers the fuller breakdown (revenue, lowest performers) on request.
    performance = await db.product_performance(connection_id, days=days, limit=5)
    report["top_products"] = performance["top_by_quantity"]
    report["product_data_has_history"] = performance["has_data"]
    return report


#: Safety cap on how many pages `_scan_catalog` will walk for one request.
#: 200/page * 250 pages = 50,000 products - comfortably past a 40,000-SKU
#: real-client catalogue (CLAUDE.md's own real-volume worked example) while
#: still bounding one HTTP request to a finite amount of adapter work. If a
#: catalogue is bigger than this, the scan stops and honestly reports
#: `complete: False` rather than hanging the request forever.
_CATALOG_SCAN_PAGE_SIZE = 200
_CATALOG_SCAN_MAX_PAGES = 250


async def _scan_catalog(connection_id: str) -> tuple[list, bool, int]:
    """Walk this connection's entire catalog, honestly.

    Every real client's catalogue can be larger than any one page a platform
    will return, so a single `search_products("", limit=100)` call silently
    truncates - correct for Northfield's ~40 demo products, wrong the moment a
    real Magento or Shopware client's 40,000-SKU catalogue is behind the same
    adapter. This walks it with `offset`/`limit` paging until a page comes
    back empty (the catalogue is exhausted) or the safety cap above is hit.

    `offset` is not on `StandardCommerceInterface.search_products` - it is an
    adapter-specific extension, the same pattern already used for `dept`
    (category browsing). Not every real platform's connector will have added
    it yet, so this checks the adapter's own signature before ever passing it;
    an adapter without the parameter gets exactly one page, and the caller is
    told the result is incomplete rather than being lied to about completeness.

    Returns `(products, complete, scanned_count)`. `complete` is False when
    either the adapter cannot page at all (one page only, and more may exist
    beyond it) or the safety cap was hit before an empty page was reached -
    both are told to the caller, never silently presented as the whole catalog.
    """
    adapter = _adapter(connection_id)
    supports_offset = "offset" in inspect.signature(adapter.search_products).parameters

    products: list = []
    offset = 0
    for page in range(_CATALOG_SCAN_MAX_PAGES):
        kwargs = {"limit": _CATALOG_SCAN_PAGE_SIZE}
        if supports_offset:
            kwargs["offset"] = offset
        result = await adapter.search_products("", **kwargs)
        products.extend(result.products)

        if not supports_offset:
            # One page is all this adapter can give us. Honest about it: complete
            # only if that one page evidently held the whole catalog.
            complete = (
                result.total_available is not None
                and result.total_available <= len(result.products)
            ) or len(result.products) < _CATALOG_SCAN_PAGE_SIZE
            return products, complete, len(products)

        if len(result.products) < _CATALOG_SCAN_PAGE_SIZE:
            # Short page: the catalog is exhausted, not merely paused.
            return products, True, len(products)

        offset += _CATALOG_SCAN_PAGE_SIZE
    else:
        # Hit the safety cap without ever seeing a short page - a genuinely
        # huge catalog (or a platform whose paging never terminates). Reported
        # as incomplete rather than pretending the cap was the whole catalog.
        return products, False, len(products)


async def _catalog_alerts(connection_id: str) -> dict:
    """Products currently out of stock or low, across the whole catalog - not
    tracked or cached anywhere, read fresh every call so an answer can never
    describe stock that has since changed.

    This is the one function both the merchant console's inventory panel and
    the merchant copilot read from (`/api/catalog/{connection_id}` and the
    `/api/copilot/{connection_id}` route both call this), so the two surfaces
    can never disagree about what is out of stock.

    Best-effort at the connectivity level, like every other catalog read in
    this codebase (see `/api/simulate`'s identical `except CommerceError:
    pass`): a platform outage degrades the answer to an honest "could not read
    the catalog right now", not a broken turn.

    Low-stock classification is never invented. It uses only the adapter's own
    `Availability.LOW_STOCK` value - already a platform-declared fact, not a
    number this code guesses at. Some real platforms have no such concept at
    all (Kettle's own `mapping.availability` never returns it - the platform
    only exposes a boolean); this is reported as `low_stock_available: False`
    rather than a silent, misleading empty list that reads as "nothing low".
    """
    adapter = _adapter(connection_id)
    try:
        products, complete, scanned = await _scan_catalog(connection_id)
    except CommerceError:
        return {
            "reachable": False,
            "complete": False,
            "scanned": 0,
            "truncated_at": None,
            "out_of_stock": [],
            "low_stock": [],
            "low_stock_available": None,
        }

    out_of_stock = [
        {"product_id": p.product_id, "sku": p.sku, "title": p.title}
        for p in products
        if p.availability == Availability.OUT_OF_STOCK
    ]
    low_stock_products = [p for p in products if p.availability == Availability.LOW_STOCK]
    # LOW_STOCK never appearing is ambiguous by itself - either genuinely
    # nothing is low right now, or this platform never expresses that state at
    # all (Kettle: boolean in-stock only, no granularity - see
    # mapping.availability's own docstring). Disambiguated by asking the
    # adapter's own declared capability constraints for CHECK_INVENTORY,
    # rather than guessing "nothing low" from an empty list.
    low_stock_available = True
    try:
        caps = await adapter.get_capabilities()
        inventory_cap = caps.operations.get(Operation.CHECK_INVENTORY)
        if inventory_cap and inventory_cap.constraints.get("stock_granularity") == "boolean":
            low_stock_available = False
    except CommerceError:
        pass
    return {
        "reachable": True,
        "complete": complete,
        "scanned": scanned,
        "truncated_at": None if complete else scanned,
        "out_of_stock": out_of_stock,
        "low_stock": [
            {"product_id": p.product_id, "sku": p.sku, "title": p.title}
            for p in low_stock_products
        ],
        "low_stock_available": low_stock_available,
    }


@app.get(f"{API}/catalog/{{connection_id}}")
async def catalog_alerts(
    connection_id: str,
    _=Depends(merchant_scoped()),
) -> dict:
    """Out-of-stock and low-stock products across the whole catalog.

    The Inventory & Catalog console panel's data source, and the exact same
    function the merchant copilot answers "what's out of stock" from - one
    fixed calculation, read from two surfaces, so the panel and the copilot's
    answer can never disagree. Scoped by merchant_scoped the same as every
    other console route, so one merchant can never read another's catalog.

    Always renders something honest rather than an empty panel or a bare
    list mistaken for the whole catalog: `reachable` is False if the
    platform could not be read at all, `complete` is False (with
    `truncated_at` set) if the scan hit its safety cap before finishing, and
    `low_stock_available` is False for a platform (like Kettle) that has no
    low-stock concept at all - never silently presented as "zero low-stock
    products found".
    """
    _adapter(connection_id)
    return await _catalog_alerts(connection_id)


@app.post(f"{API}/copilot/{{connection_id}}", response_model=CopilotAnswer)
async def merchant_copilot(
    connection_id: str,
    body: CopilotQuestion,
    _=Depends(merchant_scoped()),
) -> CopilotAnswer:
    """A merchant's own question about their own store, answered from real data.

    Read-only: proposes nothing, decides nothing, executes nothing, and never
    reaches the risk gate - there is no action here for the gate to classify.
    Scoped by merchant_scoped the same as /report and /stats, so one client
    can never ask about another's figures with their own key.

    Passes the same capabilities/policy/rules/actions data the console's other
    panels already show, via the shared `_capabilities_dict`/`_policy_dict`/
    `_rules_list`/`_actions_list` helpers - so a question like "what can this
    platform do" or "what's my current automation mode" is answerable, not
    just report-figure questions. Capabilities lookup is best-effort: a
    connection with no capability declaration yet must not break the whole
    answer over one missing category of data.
    """
    _adapter(connection_id)
    try:
        capabilities = await _capabilities_dict(connection_id)
    except HTTPException:
        capabilities = None
    reply = await copilot.ask(
        connection_id,
        body.question,
        capabilities=capabilities,
        policy=_policy_dict(connection_id),
        rules=[r.model_dump() for r in _rules_list()],
        actions=[a.model_dump() for a in _actions_list()],
        catalog_alerts=await _catalog_alerts(connection_id),
        unmet_demand=await db.unmet_demand(connection_id, days=30),
        product_performance=await db.product_performance(connection_id, days=30),
        sales_period_comparison=await db.sales_period_comparison(connection_id, days=7),
    )
    return CopilotAnswer(answer=reply.answer, used_model=reply.used_model)


@app.post(f"{API}/admin/expire")
async def run_expiry(_=Depends(operator)) -> dict:
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
async def ops_queue(_=Depends(operator)) -> dict:
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
async def ops_history(limit: int = 40, _=Depends(operator)) -> dict:
    """What has already been decided, and what came of it."""
    rows = await db.decided_across(_operator_connections(), limit=limit)
    for row in rows:
        row["merchant_name"] = MERCHANT_NAMES.get(
            row["connection_id"], row["connection_id"]
        )
    return {"decisions": rows}


@app.get(f"{API}/ops/stats")
async def ops_overview(_=Depends(operator)) -> dict:
    """Workload across every merchant."""
    stats = await db.ops_stats(_operator_connections())
    stats["by_merchant"] = {
        MERCHANT_NAMES.get(cid, cid): count
        for cid, count in stats["by_merchant"].items()
    }
    return stats


@app.get(f"{API}/ops/handovers")
async def ops_handovers(
    offset: int = 0, limit: int = 50, _=Depends(operator)
) -> dict:
    """Cases handed to a person that nobody has picked up.

    Separate from the approval queue because they are different work. An approval
    asks "may I?" and waits for an answer. A handover says "your turn" - the
    decision is made, and somebody has to act.

    Conflating them is what hid these: the queue listed approvals, escalations
    created none, and twelve shoppers were told a person would help while nobody
    knew. Oldest first, because each one is somebody waiting - and paged, because
    capping at the oldest fifty without a total hid the newest ones entirely on a
    busy system: a shopper told a person would help could sit outside the window,
    invisible until the oldest were closed. `total` is how the consumer knows more
    pages exist.
    """
    rows, total = await db.handovers_across(
        _operator_connections(), offset=max(0, offset), limit=max(1, limit)
    )
    for row in rows:
        row["merchant_name"] = MERCHANT_NAMES.get(
            row["connection_id"], row["connection_id"]
        )
    return {"handovers": rows, "total": total}


@app.post(f"{API}/ops/handovers/{{connection_id}}/{{case_id}}")
async def close_handover(
    connection_id: str,
    case_id: str,
    body: HandoverDone,
    _=Depends(operator),
) -> dict:
    """Mark a handover dealt with.

    Records who and when rather than deleting the row, so "was this ever picked
    up, and by whom" stays answerable. That question is the reason this exists.
    """
    closed = await db.mark_handled(
        connection_id, case_id, body.handled_by, body.note
    )

    if closed is None:
        # Already handled, or not this merchant's to handle. Same answer either
        # way, so an operator cannot probe another merchant's case ids.
        return {"case_id": case_id, "changed": False}

    # The shopper hears what happened. This is the point of the whole exercise:
    # they were told a person would help, a person did something, and until now
    # they heard nothing.
    if closed.get("session_id") and body.note:
        try:
            await session_store.add_turn(
                session_id=closed["session_id"],
                connection_id=connection_id,
                speaker="assistant",
                text=f"An update from the shop: {body.note}",
                case_id=case_id,
            )
        except Exception:  # noqa: BLE001
            logger.exception("could not pass an update to the shopper")

    return {"case_id": case_id, "changed": True}


@app.post(f"{API}/ops/copilot", response_model=CopilotAnswer)
async def ops_copilot(
    body: CopilotQuestion,
    _=Depends(operator),
) -> CopilotAnswer:
    """A CV3 operator's own question about workload across every merchant
    they cover, answered from real data.

    Read-only, the same shape as the merchant copilot: proposes nothing,
    decides nothing, executes nothing, and never reaches the risk gate.
    Gated by the same `operator` dependency as the rest of /api/ops/*, so
    this answers from exactly the merchants that operator can already see -
    no per-merchant key can reach it, and no wider set than /ops/queue
    itself already exposes.
    """
    reply = await copilot.ask_ops(
        body.question, _operator_connections(), MERCHANT_NAMES
    )
    return CopilotAnswer(answer=reply.answer, used_model=reply.used_model)
