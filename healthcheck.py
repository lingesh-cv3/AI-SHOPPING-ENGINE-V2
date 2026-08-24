"""End-to-end check of everything the engine is supposed to do.

Written after a session where several edits silently reverted, because files were
open in an editor while being written to on disk. The failure mode is nasty: the
code looks right when you read it, and the running system behaves as though it was
never changed.

So this does not read source. It drives the running services over HTTP and asserts
on what actually comes back, which is the only thing that cannot lie about itself.

Run with all four processes up:

    python -m uvicorn sample_merchant.api.main:app --port 8001
    python -m uvicorn sample_merchant_two.api.main:app --port 8002
    python -m uvicorn engine.api.main:app --port 8000

    python healthcheck.py

Every check prints PASS or FAIL with what was expected. A FAIL names the file to
look at.
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
import json
import uuid

ENGINE = "http://127.0.0.1:8000"
NORTHFIELD = "conn_demo"
KETTLE = "conn_kettle"

passed = 0
failed: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{ENGINE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:200]}
    except urllib.error.URLError as e:
        # A service being down is a result, not a crash. Reporting it as a failed
        # check with the reason is far more useful than a stack trace that buries
        # "connection refused" forty lines down.
        return {"_unreachable": str(e.reason)}


def check(name: str, ok: bool, detail: str = "", fix: str = "") -> bool:
    global passed
    if ok:
        passed += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    else:
        failed.append(name)
        print(f"  FAIL  {name}")
        if detail:
            print(f"        got: {detail}")
        if fix:
            print(f"        look at: {fix}")
    return ok


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ---------------------------------------------------------------------------

section("Services")

health = call("GET", "/health")
check(
    "engine is up",
    health.get("status") == "ok",
    str(health)[:90],
    "python -m uvicorn engine.api.main:app --port 8000",
)
model_on = health.get("ai_reasoning") == "active"
check(
    "model configured",
    model_on,
    str(health.get("ai_reasoning")),
    ".env needs GROQ_API_KEY",
)

if "_unreachable" in health:
    # Everything below needs the engine. Continuing would print thirty identical
    # failures and bury the one that matters.
    print("\n  The engine is not answering. Start it and run this again:")
    print("    python -m uvicorn engine.api.main:app --port 8000")
    sys.exit(1)

connections = call("GET", "/api/connections")
ids = [c["connection_id"] for c in connections] if isinstance(connections, list) else []
check(
    "both merchants registered",
    NORTHFIELD in ids and KETTLE in ids,
    str(ids),
    "engine/api/deps.py",
)

# ---------------------------------------------------------------------------

section("Platform independence")

nf = call("GET", f"/api/connections/{NORTHFIELD}/capabilities")
kb = call("GET", f"/api/connections/{KETTLE}/capabilities")

check(
    "Northfield exposes no payment recovery",
    nf.get("payment_recovery_methods") == [],
    str(nf.get("payment_recovery_methods")),
    "adapters/sample/adapter.py",
)
check(
    "Kettle exposes payment recovery",
    len(kb.get("payment_recovery_methods") or []) >= 2,
    str(kb.get("payment_recovery_methods")),
    "adapters/kettle/adapter.py",
)

nf_search = call("GET", f"/api/shop/{NORTHFIELD}/search?q=running%20shoes")
check(
    "multi-word search works",
    len(nf_search.get("products") or []) > 0,
    f"{len(nf_search.get('products') or [])} results for 'running shoes'",
    "sample_merchant/store.py::search_items",
)

dead = call("GET", f"/api/shop/{NORTHFIELD}/search?q=trainers")
check(
    "dead search still detected",
    dead.get("is_dead_search") is True,
    str(dead.get("is_dead_search")),
    "engine/api/shop.py",
)

browse = call("GET", f"/api/shop/{NORTHFIELD}/search?limit=100")
check(
    "browsing is not friction",
    browse.get("is_dead_search") is False,
    str(browse.get("is_dead_search")),
    "engine/api/shop.py",
)

kb_products = call("GET", f"/api/shop/{KETTLE}/search?limit=5")
first = (kb_products.get("products") or [{}])[0]
variants = first.get("variants") or []
check(
    "Kettle reports no stock counts",
    bool(variants) and variants[0].get("quantity_available") is None,
    f"quantity_available={variants[0].get('quantity_available') if variants else 'no variants'}",
    "adapters/kettle/mapping.py",
)

# ---------------------------------------------------------------------------

section("Risk gate")

call(
    "PUT",
    f"/api/policy/{NORTHFIELD}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": []},
)

money = call(
    "POST",
    "/api/simulate",
    {"connection_id": NORTHFIELD, "candidates": ["APPLY_PROMOTION"]},
)
check(
    "money cannot be automated",
    money.get("risk_outcome") == "HUMAN"
    and money.get("risk_rule") == "FINANCIAL_ALWAYS_HUMAN",
    f"{money.get('risk_outcome')} / {money.get('risk_rule')}",
    "engine/risk/gate.py",
)

safe = call(
    "POST",
    "/api/simulate",
    {"connection_id": NORTHFIELD, "candidates": ["SUGGEST_ALTERNATIVE"]},
)
check(
    "safe actions run with an empty allowlist",
    safe.get("risk_outcome") == "AUTO",
    f"{safe.get('risk_outcome')} / {safe.get('risk_rule')}",
    "engine/risk/gate.py rule 9",
)

blocked = call(
    "PUT",
    f"/api/policy/{NORTHFIELD}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": ["SUGGEST_ALTERNATIVE"]},
)
off = call(
    "POST",
    "/api/simulate",
    {"connection_id": NORTHFIELD, "candidates": ["SUGGEST_ALTERNATIVE"]},
)
# A switched-off action never reaches the gate: the Decision Engine drops it during
# filtering, so what arrives is an escalation. That is the correct behaviour and it
# is worth asserting precisely, because "did not get BLOCK" would look like a
# failure when the action was in fact refused.
check(
    "switching an action off stops it running",
    off.get("selected_action") == "ESCALATE_TO_HUMAN",
    f"selected {off.get('selected_action')}",
    "engine/decision/engine.py",
)
call(
    "PUT",
    f"/api/policy/{NORTHFIELD}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": []},
)

check(
    "policy persisted",
    call("GET", f"/api/policy/{NORTHFIELD}").get("mode") == "STANDARD",
    str(call("GET", f"/api/policy/{NORTHFIELD}").get("mode")),
    "engine/api/routes.py::set_policy",
)

# ---------------------------------------------------------------------------

section("Reasoning")

if model_on:
    reasoned = call(
        "POST",
        "/api/simulate",
        {
            "connection_id": NORTHFIELD,
            "friction": "DEAD_SEARCH",
            "query": "trainers",
        },
    )
    used = reasoned.get("used_model")
    check(
        "model reasons about a dead search",
        used is True,
        f"used_model={used}, fallback={reasoned.get('fallback_reason')}",
        "rate limited? wait a minute and retry",
    )
    if used:
        check(
            "diagnosis is specific",
            bool(reasoned.get("diagnosis")),
            str(reasoned.get("diagnosis"))[:70],
            "engine/reasoning/prompts.py",
        )
else:
    print("  SKIP  model checks (no key configured)")

# ---------------------------------------------------------------------------

section("Northfield: a decline that cannot be recovered")

cart = call("POST", f"/api/shop/{NORTHFIELD}/cart")
call(
    "POST",
    f"/api/shop/{NORTHFIELD}/cart/{cart['cart_id']}/lines",
    {"product_id": "P1001", "variant_id": "P1001-8", "quantity": 1},
)
paid = call(
    "POST",
    f"/api/shop/{NORTHFIELD}/cart/{cart['cart_id']}/checkout",
    {"card_last4": "0002"},
)
nf_order = (paid.get("order") or {}).get("order_id")

nf_case = call(
    "POST",
    "/api/simulate",
    {
        "connection_id": NORTHFIELD,
        "friction": "PAYMENT_DECLINED",
        "order_id": nf_order,
        "session_id": "hc_nf",
    },
)
check(
    "escalates when the platform cannot help",
    nf_case.get("selected_action") == "ESCALATE_TO_HUMAN"
    and nf_case.get("escalated_because_empty") is True,
    str(nf_case.get("selected_action")),
    "engine/decision/engine.py",
)
check(
    "the over-promise is replaced",
    nf_case.get("reply") != nf_case.get("shopper_reply"),
    "reply and shopper_reply are identical",
    "engine/api/routes.py::simulate",
)

# ---------------------------------------------------------------------------

section("Kettle: a decline that can be recovered")

call(
    "PUT",
    f"/api/policy/{KETTLE}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": []},
)

bag = call("POST", f"/api/shop/{KETTLE}/cart")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{bag['cart_id']}/lines",
    {
        "product_id": "KB-ETH-01",
        "variant_id": "KB-ETH-01::250g whole bean",
        "quantity": 1,
    },
)
kb_paid = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{bag['cart_id']}/checkout",
    {"card_last4": "0002"},
)
kb_order = (kb_paid.get("order") or {}).get("order_id")
session = f"hc_{uuid.uuid4().hex[:8]}"

kb_case = call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
        "order_id": kb_order,
    },
)
check(
    "offers recovery instead of escalating",
    kb_case.get("selected_action") in {"OFFER_ALTERNATE_PAYMENT", "SPLIT_PAYMENT"},
    str(kb_case.get("selected_action")),
    "engine/decision/ranking.py",
)
check(
    "recovery still needs a person",
    kb_case.get("awaiting_person") is True,
    str(kb_case.get("risk_rule")),
    "engine/risk/gate.py",
)

queue = call("GET", f"/api/approvals/{KETTLE}")
approvals = queue.get("approvals") or []
check(
    "case reaches the queue",
    len(approvals) > 0,
    f"{len(approvals)} pending",
    "engine/db/repository.py::record_case",
)

if approvals:
    apr = approvals[-1]["approval_id"]
    decided = call(
        "POST",
        f"/api/approvals/{KETTLE}/{apr}",
        {"approved": True, "decided_by": "healthcheck"},
    )
    executed = decided.get("executed") or {}
    check(
        "approving executes",
        executed.get("succeeded") is True,
        str(executed.get("summary"))[:80],
        "engine/execution/service.py",
    )
    check(
        "revenue is captured",
        bool(executed.get("payload", {}).get("recovered_amount")),
        str(executed.get("payload", {}).get("recovered_amount")),
        "engine/execution/service.py",
    )

    order_now = call("GET", f"/api/shop/{KETTLE}/order/{kb_order}")
    check(
        "the order is actually paid",
        order_now.get("payment_status") == "CAPTURED",
        f"{order_now.get('status')} / {order_now.get('payment_status')}",
        "adapters/kettle/adapter.py::recover_payment",
    )

    again = call(
        "POST",
        f"/api/approvals/{KETTLE}/{apr}",
        {"approved": True, "decided_by": "someone else"},
    )
    check(
        "a second approval changes nothing",
        again.get("changed") is False,
        str(again.get("changed")),
        "engine/db/repository.py::decide_approval",
    )

    transcript = call("GET", f"/api/chat/{KETTLE}/{session}")
    turns = transcript.get("turns") or []
    told = any("gone through" in (t.get("text") or "") for t in turns)
    check(
        "the shopper is told the outcome",
        told,
        f"{len(turns)} turns, none reporting the recovery",
        "engine/execution/service.py delivery block",
    )

# ---------------------------------------------------------------------------

section("Shared memory")

mem = call(
    "POST",
    "/api/chat",
    {"connection_id": KETTLE, "session_id": session, "message": "what now?"},
)
check(
    "friction is remembered across surfaces",
    (mem.get("remembered_friction") or 0) > 0,
    f"friction={mem.get('remembered_friction')}, turns={mem.get('remembered_turns')}",
    "engine/session/store.py",
)

# ---------------------------------------------------------------------------

section("Reporting")

report = call("GET", f"/api/report/{KETTLE}")
check(
    "merchant report returns figures",
    "revenue_recovered" in report,
    str(report)[:80],
    "engine/db/repository.py::merchant_report",
)
if "revenue_recovered" in report:
    check(
        "revenue is non-zero",
        report["revenue_recovered"] != "0.00",
        report["revenue_recovered"],
        "engine/execution/service.py record_outcome",
    )
    check(
        "recent activity is listed",
        len(report.get("recent") or []) > 0,
        f"{len(report.get('recent') or [])} entries",
        "engine/db/repository.py::merchant_report",
    )

stats = call("GET", f"/api/stats/{KETTLE}")
check(
    "operations stats available",
    "cases" in stats,
    str(stats)[:80],
    "engine/db/repository.py::stats",
)

# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
if failed:
    print(f"{passed} passed, {len(failed)} FAILED")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)

print(f"All {passed} checks passed.")