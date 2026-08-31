"""Reset to a clean demo state.

Run this a few minutes before the demo, with all four processes up.

Two things it fixes. The database accumulates every test run, so the operations
queue fills with expired approvals and healthcheck noise that makes the product
look chaotic. And an empty database is worse - the merchant report reads as zeros,
which demos as "nothing works" rather than "nothing has happened yet".

So this leaves exactly enough history to look real:

  - both merchants set to Standard
  - one payment recovered on Kettle, so the merchant report shows real revenue
  - one approval left pending, so the operations queue has something to work
  - one dead search on Northfield, so the friction breakdown is not empty

It does not touch cv3.db directly - everything goes through the engine's own API,
so whatever it produces is something the product genuinely did.

    python demo_reset.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

ENGINE = "http://127.0.0.1:8000"
NORTHFIELD = "conn_demo"
KETTLE = "conn_kettle"


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