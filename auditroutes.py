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

Then a fourth question, asked of the shopper routes only:

  refused with another *shopper's* key?

Which is a different question from the third and was not being asked. A publishable
key is bound to one merchant and ships in the browser, so every shopper at a shop
holds the same one. Merchant isolation says nothing about whether one of them can
read another's basket, and until it was probed the answer was that they could.

The scoping section drives two cookie jars against the same publishable key - two
browsers, one shop - and checks that what the first one made is refused to the
second. It creates a cart and one order per run, which is the price of testing the
real thing rather than a description of it.

    python auditroutes.py
"""

import http.cookiejar
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
    except TimeoutError:
        # Reported as timed out rather than crashing the run. One slow route
        # should not cost the other fourteen their check - and the reason this
        # happened is that a probe waited on the model, which an audit never
        # should.
        return -1


class Browser:
    """One caller, with its own cookie jar.

    A cookie jar rather than a bare request, because that is what makes two of
    these two different shoppers. They hold the same publishable key - every
    shopper at a shop does - so anything that tells them apart has to come from
    the browser rather than from the key.
    """

    def __init__(self, key: str) -> None:
        self.key = key
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def call(self, method: str, path: str, body=None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{ENGINE}{path}",
            data=data,
            method=method,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.key}",
            },
        )
        try:
            with self.opener.open(req, timeout=25) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if raw else None)
        except urllib.error.HTTPError as e:
            return e.code, None
        except (urllib.error.URLError, TimeoutError):
            return 0, None


#: What counts as "that is not yours".
#:
#: 404 is the one to want. It refuses without confirming the thing exists, which
#: 403 would - and an id that answers "forbidden" is an id somebody now knows is
#: real. 401 and 403 are accepted here because they are still refusals, and an
#: audit that failed a working lock over its status code would be noise.
REFUSED_CODES = {401, 403, 404}


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
    # Reads a transcript rather than sending a message. Same router, same guard,
    # and no model call - an audit that waits on a provider is one that cannot be
    # run when the provider is busy, which is exactly when somebody might want to
    # check whether the doors are shut.
    (
        "chat transcript",
        "GET",
        "/api/chat/conn_kettle/audit-probe",
        PK_KETTLE,
        PK_DEMO,
        None,
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
    if no_key == -1:
        notes.append("timed out")
    elif no_key != 401:
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
print("  Shopper scoping")
print()


def scoping() -> list[tuple[str, list[str]]]:
    """Can one shopper reach another's things, holding the key they both hold?

    Two browsers at the same shop. The first makes a basket, talks to the
    assistant and buys something; the second - a stranger with the same
    publishable key, guessing at an id - tries to reach all three.

    Ids here are sequential (BSK00001, BSK00002), so "guessing" is counting.
    That is what makes this worth probing rather than assuming.
    """
    found: list[tuple[str, list[str]]] = []

    def refused(label: str, code: int, what: str) -> None:
        if code in REFUSED_CODES:
            print(f"    ok    {label:22} refused a stranger ({code})")
        elif code == 0:
            found.append((label, ["engine did not answer"]))
            print(f"    FAIL  {label:22} engine did not answer")
        else:
            found.append((label, [f"{what} ({code})"]))
            print(f"    FAIL  {label:22} {what} ({code})")

    mine = Browser(PK_DEMO)
    stranger = Browser(PK_DEMO)

    code, cart = mine.call("POST", "/api/shop/conn_demo/cart")
    if code != 200 or not isinstance(cart, dict):
        found.append(("shopper scoping", [f"could not make a cart to test with ({code})"]))
        print(f"    FAIL  {'setup':22} could not make a cart to test with ({code})")
        return found

    cart_id = cart["cart_id"]
    mine.call(
        "POST",
        f"/api/shop/conn_demo/cart/{cart_id}/lines",
        {"product_id": "P1003", "quantity": 1},
    )

    code, _ = stranger.call("GET", f"/api/shop/conn_demo/cart/{cart_id}")
    refused("another's cart", code, "read it")

    code, _ = stranger.call(
        "POST",
        f"/api/shop/conn_demo/cart/{cart_id}/lines",
        {"product_id": "P1002", "quantity": 1},
    )
    refused("another's cart, write", code, "added a line to it")

    # The tap endpoint rather than a message, for the same reason the transcript
    # probe exists: no model call, so the audit runs while the provider is busy.
    session_id = "audit-scope-probe"
    mine.call(
        "POST",
        "/api/chat/act",
        {
            "connection_id": "conn_demo",
            "session_id": session_id,
            "product_id": "P1003",
            "cart_id": cart_id,
            "said": "Add that",
        },
    )

    code, _ = stranger.call("GET", f"/api/chat/conn_demo/{session_id}")
    refused("another's conversation", code, "read the transcript")

    code, paid = mine.call(
        "POST", f"/api/shop/conn_demo/cart/{cart_id}/checkout", {"card_last4": "1111"}
    )
    order_id = (paid or {}).get("order", {}).get("order_id") if isinstance(paid, dict) else None

    if not order_id:
        note = f"could not make an order to test with ({code})"
        found.append(("another's order", [note]))
        print(f"    FAIL  {'another order':22} {note}")
        return found

    code, _ = stranger.call("GET", f"/api/shop/conn_demo/order/{order_id}")
    refused("another's order", code, "read it")

    return found


problems.extend(scoping())

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
print("  another merchant's key exists, refuses that too. One shopper cannot")
print("  reach another's basket, conversation or order.")
print()
