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
from pathlib import Path
import json
import uuid

ENGINE = "http://127.0.0.1:8000"


def load_keys() -> dict[str, str]:
    """The keys mint_keys.py wrote, or an empty dict.

    Missing keys are not a failure of the engine, so the auth-dependent checks skip
    with a reason rather than reporting red. A red line should mean something is
    broken, not that a setup step has not been run.
    """
    path = Path(".env.keys")
    if not path.exists():
        return {}

    found: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        found[name.strip()] = value.strip()
    return found


KEYS = load_keys()
OPERATOR = KEYS.get("CV3_OPERATOR_KEY")
SECRETS = {
    "conn_demo": KEYS.get("CV3_SECRET_CONN_DEMO"),
    "conn_kettle": KEYS.get("CV3_SECRET_CONN_KETTLE"),
}


#: Passed by a check that must reach the engine unauthenticated. An empty string
#: was used first and is falsy, so it fell through to the automatic lookup and
#: the request arrived with a key - which made a locked route report as open.
NO_KEY = "__none__"


def key_for(path: str, body: dict | None) -> str | None:
    """The key a call should carry, worked out from where it is going.

    Chosen here rather than passed at every call site. There are around sixty calls
    in this file, and threading a key through each would be sixty chances to forget
    one - which surfaces as a mysterious failure rather than a missing argument.

    The secret key is used throughout rather than the publishable one. A secret key
    can do everything a publishable key can, so one lookup covers every route, and
    the audit is what proves a publishable key is correctly limited.
    """
    for cid in ("conn_kettle", "conn_demo"):
        if f"/{cid}" in path:
            return SECRETS.get(cid)

    if body and isinstance(body, dict):
        cid = body.get("connection_id")
        if cid:
            return SECRETS.get(cid)

    if "/ops/" in path or "/admin/" in path:
        return OPERATOR

    return None
NORTHFIELD = "conn_demo"
KETTLE = "conn_kettle"

passed = 0
failed: list[str] = []


def call(
    method: str, path: str, body: dict | None = None, key: str | None = None
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{ENGINE}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            # An explicit key wins, so a check that deliberately sends none - or
            # sends the wrong one - still gets to do that.
            **(
                {}
                if key == NO_KEY
                else (
                    {"Authorization": f"Bearer {key}"}
                    if key
                    else (
                        {"Authorization": f"Bearer {auto}"}
                        if (auto := key_for(path, body))
                        else {}
                    )
                )
            ),
        },
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


#: What the assistant says when the provider refused us. It means one thing
#: only, so it is safe to key on.
#: Keyed on the shortest phrase in the throttle message unlikely to be reworded.
#:
#: The message has already changed once - from "getting" to "handling" - and this
#: check silently stopped working, which is the risk in matching prose at all. If it
#: is reworded again, this stops recognising a skip and reports a failure: annoying,
#: and the safe direction.
THROTTLED = "a lot of questions"

#: Checks that could not run. Counted apart from failures, because a red line
#: should mean something is broken.
skipped: list[str] = []


def check(name: str, ok: bool, detail: str = "", fix: str = "") -> bool:
    global passed
    if ok:
        passed += 1
        print(f"  PASS  {name}" + (f"  ({detail})" if detail else ""))
    elif THROTTLED in detail:
        # Throttled is not failed. The model was never asked, so nothing
        # about the engine was measured either way.
        skipped.append(name)
        print(f"  SKIP  {name}  (the model was busy, not checked)")
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

# A refused first call means the keys are missing or stale. Better to say so than
# to let sixty checks fail and then crash indexing into a refusal.
probe = call("GET", "/api/shop/conn_demo/departments")
if probe.get("_status") == 401:
    print()
    print("  The engine refused an authenticated request.")
    print("  .env.keys is missing or does not match this database.")
    print("  Delete cv3.db, restart the engine, and run mint_keys.py again.")
    print()
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

# Tested while the problem is still open, which is the moment that matters: a card
# declines, the shopper types "what now", and the assistant already knows without
# being told. A resolved problem is deliberately excluded - otherwise a shopper whose
# payment had just been recovered said hello and was told someone needed to approve
# something.
open_session = f"hc_mem_{uuid.uuid4().hex[:8]}"
open_bag = call("POST", f"/api/shop/{KETTLE}/cart")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{open_bag['cart_id']}/lines",
    {
        "product_id": "KB-COL-02",
        "variant_id": "KB-COL-02::250g whole bean",
        "quantity": 1,
    },
)
open_paid = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{open_bag['cart_id']}/checkout",
    {"card_last4": "0002"},
)
call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": open_session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
        "order_id": (open_paid.get("order") or {}).get("order_id"),
    },
)

mem = call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": open_session,
        "message": "what now?",
    },
)
check(
    "an open problem is remembered across surfaces",
    (mem.get("remembered_friction") or 0) > 0,
    f"friction={mem.get('remembered_friction')}, turns={mem.get('remembered_turns')}",
    "engine/session/store.py",
)

resolved = call(
    "POST",
    "/api/chat",
    {"connection_id": KETTLE, "session_id": session, "message": "hello again"},
)
check(
    "a resolved problem is not",
    (resolved.get("remembered_friction") or 0) == 0,
    f"friction={resolved.get('remembered_friction')} on a session whose case closed",
    "engine/session/store.py::recent_friction",
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

section("Cart, through the chat")

if model_on:
    shop_session = f"hc_cart_{uuid.uuid4().hex[:6]}"
    shop_cart = call("POST", f"/api/shop/{NORTHFIELD}/cart")

    # A product with several sizes. Adding it without one should ask rather than
    # guess, because a guessed size is a return waiting to happen.
    ask = call(
        "POST",
        "/api/chat",
        {
            "connection_id": NORTHFIELD,
            "session_id": shop_session,
            "message": "add the Trailblazer Running Shoe to my cart",
            "cart_id": shop_cart["cart_id"],
        },
    )
    choices = ask.get("choices") or []
    check(
        "asks which size instead of guessing",
        len(choices) > 0,
        f"{len(choices)} choices, reply: {str(ask.get('reply'))[:60]}",
        "engine/execution/service.py ADD_TO_CART, engine/api/chat.py needs_choice",
    )

    if choices:
        check(
            "sold-out sizes are not offered",
            all(c.get("label") != "10" for c in choices),
            str([c.get("label") for c in choices]),
            "engine/execution/service.py buyable filter",
        )

    # Now with a size named.
    sized = call(
        "POST",
        "/api/chat",
        {
            "connection_id": NORTHFIELD,
            "session_id": shop_session,
            # Sent the way the widget sends it when a shopper taps an option.
            # Typing a phrase deliberately does not add - only the exact label does
            # - because fuzzy matching is what produced double-adds.
            "message": choices[0]["label"] if choices else "8",
            "cart_id": shop_cart["cart_id"],
            "known": {
                "chosen_variant": choices[0]["variant_id"] if choices else ""
            },
        },
    )
    cart_now = call("GET", f"/api/shop/{NORTHFIELD}/cart/{shop_cart['cart_id']}")
    added = cart_now.get("item_count", 0) > 0
    check(
        "adds to cart when a size is given",
        added,
        f"{cart_now.get('item_count')} items, reply: {str(sized.get('reply'))[:60]}",
        "engine/execution/service.py ADD_TO_CART",
    )
    if added:
        check(
            "the storefront is told the cart changed",
            sized.get("cart_changed") is True,
            str(sized.get("cart_changed")),
            "engine/api/chat.py cart_changed",
        )

        removed = call(
            "POST",
            "/api/chat",
            {
                "connection_id": NORTHFIELD,
                "session_id": shop_session,
                "message": "actually remove that from my cart",
                "cart_id": shop_cart["cart_id"],
            },
        )
        after = call("GET", f"/api/shop/{NORTHFIELD}/cart/{shop_cart['cart_id']}")
        check(
            "removes from cart on request",
            after.get("item_count", 1) == 0,
            f"{after.get('item_count')} items left, reply: {str(removed.get('reply'))[:60]}",
            "engine/execution/service.py REMOVE_CART_LINE",
        )
else:
    print("  SKIP  cart checks (no key configured)")

# ---------------------------------------------------------------------------

section("Rejection")

rej_bag = call("POST", f"/api/shop/{KETTLE}/cart")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{rej_bag['cart_id']}/lines",
    {
        "product_id": "KB-COL-02",
        "variant_id": "KB-COL-02::250g whole bean",
        "quantity": 1,
    },
)
rej_paid = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{rej_bag['cart_id']}/checkout",
    {"card_last4": "0002"},
)
rej_order = (rej_paid.get("order") or {}).get("order_id")
rej_session = f"hc_rej_{uuid.uuid4().hex[:6]}"

call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": rej_session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
        "order_id": rej_order,
    },
)

queue2 = (call("GET", f"/api/approvals/{KETTLE}").get("approvals")) or []
if queue2:
    rej_id = queue2[-1]["approval_id"]
    call(
        "POST",
        f"/api/approvals/{KETTLE}/{rej_id}",
        {
            "approved": False,
            "decided_by": "healthcheck",
            "note": "customer already paid by transfer",
        },
    )

    turns2 = (call("GET", f"/api/chat/{KETTLE}/{rej_session}").get("turns")) or []
    told_no = any(
        "not able to do that one" in (t.get("text") or "") for t in turns2
    )
    check(
        "a rejected shopper is told",
        told_no,
        f"{len(turns2)} turns, none reporting the rejection",
        "engine/api/routes.py::decide, the not-approved branch",
    )

    leaked = any(
        "already paid by transfer" in (t.get("text") or "") for t in turns2
    )
    check(
        "the operator's note stays private",
        not leaked,
        "the internal note reached the shopper",
        "engine/api/routes.py::decide",
    )
else:
    check("rejection case reached the queue", False, "queue was empty", "chat.py")

# ---------------------------------------------------------------------------

section("Expiry")

exp_session = f"hc_exp_{uuid.uuid4().hex[:6]}"
call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": exp_session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
    },
)

# Backdate whatever is pending so the sweeper has something to find. Written in
# SQLAlchemy's SQLite format; an ISO string with an offset compares as text and
# silently never matches.
try:
    import sqlite3
    from datetime import UTC, datetime, timedelta

    conn = sqlite3.connect("cv3.db")
    past = (datetime.now(UTC) - timedelta(minutes=30)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    conn.execute(
        "update approvals set expires_at=? where state='PENDING'", (past,)
    )
    conn.commit()
    conn.close()
    backdated = True
except Exception as e:  # noqa: BLE001
    backdated = False
    print(f"  SKIP  expiry (could not backdate: {e})")

if backdated:
    swept = call("POST", "/api/admin/expire")
    check(
        "the sweeper expires stale approvals",
        (swept.get("expired") or 0) > 0,
        str(swept),
        "engine/expiry.py, engine/db/repository.py::expire_approvals",
    )

    exp_turns = (call("GET", f"/api/chat/{KETTLE}/{exp_session}").get("turns")) or []
    check(
        "an abandoned shopper is told",
        any("in time" in (t.get("text") or "") for t in exp_turns),
        f"{len(exp_turns)} turns, none reporting the timeout",
        "engine/expiry.py::sweep_once",
    )

# ---------------------------------------------------------------------------

section("Operations console")

if OPERATOR is None:
    print("  SKIP  operations checks (no .env.keys - run mint_keys.py)")
else:
    # Explicitly keyless: this check exists to prove the door is shut.
    refused = call("GET", "/api/ops/stats", key=NO_KEY)
    check(
        "the operations queue is locked",
        # 401 specifically, not merely "it failed". A 500 that happened to stop the
        # request would otherwise read as a working lock.
        refused.get("_status") == 401,
        f"an unauthenticated request returned {refused}",
        "engine/api/auth.py",
    )

ops = call("GET", "/api/ops/stats", key=OPERATOR)
check(
    "cross-merchant workload",
    "waiting" in ops and "by_merchant" in ops,
    str(ops)[:90],
    "engine/api/routes.py::ops_overview",
)

ops_q = call("GET", "/api/ops/queue", key=OPERATOR)
q_rows = ops_q.get("approvals")
check(
    "one queue across every merchant",
    isinstance(q_rows, list),
    str(ops_q)[:90],
    "engine/api/routes.py::ops_queue",
)
if isinstance(q_rows, list) and q_rows:
    check(
        "queue entries name their merchant",
        all(r.get("merchant_name") for r in q_rows),
        "an entry has no merchant_name",
        "engine/api/routes.py::ops_queue",
    )
    check(
        "queue entries show the wait",
        all(r.get("waiting_minutes") is not None for r in q_rows),
        "an entry has no waiting_minutes",
        "engine/db/repository.py::pending_across",
    )

hist = call("GET", "/api/ops/history", key=OPERATOR)
rows = hist.get("decisions") or []
check(
    "decided work is visible",
    len(rows) > 0,
    f"{len(rows)} decisions",
    "engine/api/routes.py::ops_history",
)
if rows:
    check(
        "rejection notes are kept",
        any(r.get("note") for r in rows),
        "no decision carries a note",
        "engine/db/repository.py::decided_across",
    )
    check(
        "expiries are recorded as decisions",
        any(r.get("state") == "EXPIRED" for r in rows),
        str([r.get("state") for r in rows][:6]),
        "engine/db/repository.py::expire_approvals",
    )

# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
if skipped:
    print()
    print(f"  {len(skipped)} check(s) skipped - the model was busy:")
    for name in skipped:
        print(f"    {name}")
    print("  Not failures. Rerun when the provider is idle for a complete number.")

if failed:
    print(f"{passed} passed, {len(failed)} FAILED")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)

print(f"All {passed} checks passed.")