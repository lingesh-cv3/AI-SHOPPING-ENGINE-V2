"""Probe every route on the running engine and report what it actually does.

The first version read source files, and reported every route locked while the
engine was serving Kettle's catalogue to anyone who asked - because a process was
running older code. It was describing the repository, not the system.

So this sends real requests. Slower, and it cannot be wrong in that particular way.

Three questions per route:

  refused without a key?            the lock exists
  accepted with the right key?      the lock is not simply broken
  refused with another merchant's?  the lock checks who you are, not just that
                                    you have something

The third is the one that matters. A lock that accepts any key is a door that opens
for anyone holding a key to anywhere.

    python auditroutes.py
"""

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENGINE = "http://127.0.0.1:8000"


def keys() -> dict:
    path = Path(".env.keys")
    if not path.exists():
        print()
        print("  .env.keys not found. Run mint_keys.py first.")
        sys.exit(1)
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip()
    return out


K = keys()
PK_DEMO = K.get("CV3_PUBLISHABLE_CONN_DEMO")
PK_KETTLE = K.get("CV3_PUBLISHABLE_CONN_KETTLE")
SK_DEMO = K.get("CV3_SECRET_CONN_DEMO")
OPERATOR = K.get("CV3_OPERATOR_KEY")


def status(method: str, path: str, key=None, body=None) -> int:
    data = json.dumps(body).encode() if body is not None else None
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
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except urllib.error.URLError:
        return 0


# (label, method, path, right key, a wrong key that must be refused, body)
PROBES = [
    ("shop search", "GET", "/api/shop/conn_kettle/search?q=coffee", PK_KETTLE, PK_DEMO, None),
    ("shop product", "GET", "/api/shop/conn_kettle/product/KB-ETH-01", PK_KETTLE, PK_DEMO, None),
    ("shop departments", "GET", "/api/shop/conn_demo/departments", PK_DEMO, PK_KETTLE, None),
    ("create cart", "POST", "/api/shop/conn_kettle/cart", PK_KETTLE, PK_DEMO, None),
    ("capabilities", "GET", "/api/connections/conn_demo/capabilities", SK_DEMO, None, None),
    ("read policy", "GET", "/api/policy/conn_demo", SK_DEMO, None, None),
    ("merchant report", "GET", "/api/report/conn_demo", SK_DEMO, None, None),
    ("approval queue", "GET", "/api/approvals/conn_demo", SK_DEMO, None, None),
    ("case history", "GET", "/api/cases/conn_demo", SK_DEMO, None, None),
    ("merchant stats", "GET", "/api/stats/conn_demo", SK_DEMO, None, None),
    ("ops queue", "GET", "/api/ops/queue", OPERATOR, SK_DEMO, None),
    ("ops history", "GET", "/api/ops/history", OPERATOR, SK_DEMO, None),
    ("ops stats", "GET", "/api/ops/stats", OPERATOR, SK_DEMO, None),
    ("expiry sweep", "POST", "/api/admin/expire", OPERATOR, SK_DEMO, None),
    (
        "chat",
        "POST",
        "/api/chat",
        PK_KETTLE,
        PK_DEMO,
        {"connection_id": "conn_kettle", "session_id": "audit", "message": "hi"},
    ),
]

PUBLIC = [
    ("health", "GET", "/health"),
    ("connections", "GET", "/api/connections"),
    ("risk rules", "GET", "/api/policy/rules"),
    ("action list", "GET", "/api/policy/actions"),
]

print()

if status("GET", "/health") == 0:
    print("  The engine is not answering on port 8000.")
    print()
    sys.exit(1)

problems = []

print("  Locked routes")
print()

for label, method, path, right, wrong, body in PROBES:
    no_key = status(method, path, None, body)
    with_right = status(method, path, right, body) if right else None
    with_wrong = status(method, path, wrong, body) if wrong else None

    notes = []
    if no_key != 401:
        notes.append(f"open without a key ({no_key})")
    if with_right is not None and with_right == 401:
        notes.append("refuses its own key")
    if with_wrong is not None and with_wrong != 401:
        notes.append(f"accepts the wrong merchant's key ({with_wrong})")

    if notes:
        problems.append((label, notes))
        print(f"    FAIL  {label:20} " + "; ".join(notes))
    else:
        detail = "refused without a key, accepted with it"
        if with_wrong is not None:
            detail += ", refused another merchant's"
        print(f"    ok    {label:20} {detail}")

print()
print("  Deliberately public")
print()
for label, method, path in PUBLIC:
    code = status(method, path)
    mark = "ok  " if code == 200 else "FAIL"
    if code != 200:
        problems.append((label, [f"returned {code}"]))
    print(f"    {mark}  {label:20} {code}")

print()
if problems:
    print(f"  {len(problems)} problem(s). This is the live engine, not the source,")
    print("  so a restart will not change these numbers.")
    print()
    sys.exit(1)

print("  Every locked route refuses without a key, accepts its own, and where")
print("  another merchant's key exists, refuses that too.")
print()
