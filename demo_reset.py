"""Reset to a clean demo state.

Run this a few minutes before the demo, with all four processes up.

Two things it fixes. The database accumulates every test run, so the operations
queue fills with expired approvals and healthcheck noise that makes the product
look chaotic. And an empty database is worse - the merchant report reads as zeros,
which demos as "nothing works" rather than "nothing has happened yet".

So this leaves exactly enough history to look real:

  - both merchants set to Standard
  - one payment recovered on Kettle, so the merchant report shows real revenue
  - one recovery approved but refused by the platform, so the operations
    console can show an honest "approved, did not go through" outcome
  - one approval left pending, so the operations queue has something to work
  - one dead search on Northfield, so the friction breakdown is not empty

It does not touch cv3.db directly - everything goes through the engine's own API,
so whatever it produces is something the product genuinely did.

    python demo_reset.py
"""

from __future__ import annotations

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ENGINE = "http://127.0.0.1:8000"
NORTHFIELD = "conn_demo"
KETTLE = "conn_kettle"


def load_keys() -> dict[str, str]:
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


def key_for(path: str, body: dict | None) -> str | None:
    """The key a call should carry, worked out from where it is going.

    The same scheme healthcheck.py uses. The secret key can do everything a
    publishable key can, so one lookup covers every route that is not the
    operator's own queue. Without this every locked route here returns 401 -
    which is exactly what happened once the policy route was locked after this
    file was written.
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


#: One cookie jar for the whole run. The engine files a cart under whichever
#: browser made it, so a fresh opener per call would arrive as a different
#: shopper every time and every second call would be refused - correctly.
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def signed_in_for(shop: str) -> None:
    """Make sure the shared browser is signed in at `shop`, with an email.

    Checkout now requires a signed-in account with an email - the order
    confirmation has to reach somebody, and the checkout route refuses a guest.
    Everything below that places an order goes through checkout, so the jar is
    signed up (the whole run is one browser, so one account is the honest shape)
    and every cart the run creates afterwards is filed under that account.
    """
    username = f"demo_{uuid.uuid4().hex[:6]}"
    r = call(
        "POST",
        "/api/account/signup",
        {
            "connection_id": shop,
            "username": username,
            "password": "password123",
            "email": f"{username}@example.com",
            "guest_session": None,
            "guest_cart": None,
        },
    )
    if "_error" in r:
        die(f"could not sign up for the demo checkout: {r}")


def call(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    key = key_for(path, body)
    req = urllib.request.Request(
        f"{ENGINE}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            **({"Authorization": f"Bearer {key}"} if key else {}),
        },
    )
    try:
        with _OPENER.open(req, timeout=45) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode()[:120]}"}
    except urllib.error.URLError as e:
        return {"_unreachable": str(e.reason)}


def die(message: str) -> None:
    print()
    print(f"  STOPPED: {message}")
    print()
    sys.exit(1)


print()
health = call("GET", "/health")
if "_unreachable" in health:
    die(
        "the engine is not answering on port 8000.\n"
        "           Start all four processes, then run this again."
    )

print(f"  engine       {health.get('status')}, reasoning {health.get('ai_reasoning')}")

if health.get("ai_reasoning") != "active":
    print()
    print("  WARNING: no model key is loaded, so the assistant will fall back to")
    print("           fixed rules. Check .env before demoing the chat.")

connections = call("GET", "/api/connections")
ids = [c["connection_id"] for c in connections] if isinstance(connections, list) else []
if NORTHFIELD not in ids or KETTLE not in ids:
    die(f"both merchants are not registered. Found: {ids}")
print(f"  merchants    {', '.join(ids)}")

# ---------------------------------------------------------------------------
# Standard on both, nothing switched off.
# ---------------------------------------------------------------------------

for cid in (NORTHFIELD, KETTLE):
    r = call(
        "PUT",
        f"/api/policy/{cid}",
        {"mode": "STANDARD", "auto_allowed": [], "blocked": []},
    )
    if r.get("mode") != "STANDARD":
        die(f"could not set {cid} to Standard: {r}")
print("  policy       both merchants on Standard")

# ---------------------------------------------------------------------------
# One recovered payment on Kettle, so the merchant report has real revenue.
# ---------------------------------------------------------------------------

signed_in_for(KETTLE)
bag = call("POST", f"/api/shop/{KETTLE}/cart")
if "cart_id" not in bag:
    die(f"could not create a Kettle cart: {bag}")

call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{bag['cart_id']}/lines",
    {
        "product_id": "KB-ETH-01",
        "variant_id": "KB-ETH-01::250g whole bean",
        "quantity": 1,
    },
)
paid = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{bag['cart_id']}/checkout",
    {"card_last4": "0002"},
)
order = (paid.get("order") or {}).get("order_id")
if not order:
    die(f"the Kettle checkout did not produce an order: {paid}")

call(
    "POST",
    "/api/simulate",
    {
        "connection_id": KETTLE,
        "friction": "PAYMENT_DECLINED",
        "order_id": order,
        "session_id": "demo_history_1",
    },
)

queue = (call("GET", f"/api/approvals/{KETTLE}").get("approvals")) or []
if not queue:
    die("the declined payment did not reach the approval queue")

decided = call(
    "POST",
    f"/api/approvals/{KETTLE}/{queue[-1]['approval_id']}",
    {"approved": True, "decided_by": "cv3-operator"},
)
executed = decided.get("executed") or {}
if not executed.get("succeeded"):
    die(f"the recovery did not execute: {executed}")

recovered = executed.get("payload", {}).get("recovered_amount")
print(f"  history      recovered {recovered} on {order}")

# ---------------------------------------------------------------------------
# One approved recovery that then fails on the platform, so the operations
# console can show the honest "approved, but did not go through" outcome.
# Card 0006 is hard-blocked: it declines at checkout, a person approves a
# recovery, and the platform refuses it anyway.
# ---------------------------------------------------------------------------

hcb_bag = call("POST", f"/api/shop/{KETTLE}/cart")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{hcb_bag['cart_id']}/lines",
    {
        "product_id": "KB-COL-02",
        "variant_id": "KB-COL-02::250g whole bean",
        "quantity": 1,
    },
)
hcb_paid = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{hcb_bag['cart_id']}/checkout",
    {"card_last4": "0006"},
)
hcb_order = (hcb_paid.get("order") or {}).get("order_id")
if not hcb_order:
    die(f"the hard-blocked Kettle checkout did not produce an order: {hcb_paid}")

call(
    "POST",
    "/api/simulate",
    {
        "connection_id": KETTLE,
        "friction": "PAYMENT_DECLINED",
        "order_id": hcb_order,
        "session_id": "demo_history_hardblock",
    },
)
hcb_queue = (call("GET", f"/api/approvals/{KETTLE}").get("approvals")) or []
if not hcb_queue:
    die("the hard-blocked payment did not reach the approval queue")
hcb_decided = call(
    "POST",
    f"/api/approvals/{KETTLE}/{hcb_queue[-1]['approval_id']}",
    {"approved": True, "decided_by": "cv3-operator"},
)
hcb_executed = hcb_decided.get("executed") or {}
if hcb_executed.get("succeeded"):
    die(f"a hard-blocked recovery should not succeed: {hcb_executed}")
print(
    f"  history      {hcb_order} approved but did not go through "
    f"({hcb_executed.get('error_code')})"
)

# ---------------------------------------------------------------------------
# One dead search on Northfield, so the friction breakdown is not empty.
# ---------------------------------------------------------------------------

call(
    "POST",
    "/api/simulate",
    {
        "connection_id": NORTHFIELD,
        "friction": "DEAD_SEARCH",
        "query": "trainers",
        "session_id": "demo_history_2",
    },
)
print("  history      one dead search on Northfield")

# ---------------------------------------------------------------------------
# One approval left pending, so the operations queue has work in it.
# ---------------------------------------------------------------------------

bag2 = call("POST", f"/api/shop/{KETTLE}/cart")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{bag2['cart_id']}/lines",
    {
        "product_id": "KB-COL-02",
        "variant_id": "KB-COL-02::250g whole bean",
        "quantity": 1,
    },
)
paid2 = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{bag2['cart_id']}/checkout",
    {"card_last4": "0002"},
)
order2 = (paid2.get("order") or {}).get("order_id")
call(
    "POST",
    "/api/simulate",
    {
        "connection_id": KETTLE,
        "friction": "PAYMENT_DECLINED",
        "order_id": order2,
        "session_id": "demo_history_3",
    },
)

waiting = call("GET", "/api/ops/stats").get("waiting", 0)
print(f"  queue        {waiting} waiting for a decision")

# ---------------------------------------------------------------------------
# What the consoles will show.
# ---------------------------------------------------------------------------

report = call("GET", f"/api/report/{KETTLE}")
print()
print("  The merchant console for Kettle will show:")
print(f"    sales recovered      {report.get('revenue_recovered')} {report.get('currency')}")
print(f"    shoppers helped      {report.get('shoppers_helped')}")
print(f"    problems solved      {report.get('problems_solved')}")
print(f"    waiting on you       {report.get('waiting_for_you')}")

print()
print("  Ready. Two things before you present:")
print("    1. Hard reload the browser with Ctrl+Shift+R.")
print("    2. Do not run practice chat turns - the free tier throttles at about")
print("       four a minute, and taps are free but typing is not.")
print()