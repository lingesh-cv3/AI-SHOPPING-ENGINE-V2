"""POST /api/v2/chat/mission/advance - explicit security and behaviour checks.

The route reads the caller's own cart and moves an open guided mission on; it
never changes a cart. This probes the RUNNING engine (like auditroutes.py and
test_tenant_isolation_v2.py - a request that is really sent cannot be wrong the
way reading source can) for:

  registration         the route is present in the served OpenAPI schema (POST)
  authentication       refused without a key, with a garbage key, with a key for
                       another merchant, and for an unknown connection
  validation           missing / mistyped fields are refused (422), never 500
  ownership            another VISITOR (same publishable key) is refused with the
                       owner's real session+cart, a visitor is refused a cart or
                       session that is someone else's, the other merchant's cart
                       is refused, and a refusal reveals nothing
  no first-touch claim an unknown session id / cart id is refused and is NOT
                       claimed by the call (a real first chat turn still claims it)
  behaviour            the owner gets the confirmation + next stage once; a replay
                       returns nothing; the cart is identical before and after;
                       an attacker's refused call does not consume the owner's
                       pending confirmation

Needs the engine (8000, ideally on an isolated DB and with empty provider keys),
Northfield (8001), Kettle (8002) and .env.keys.

    python tests/shopper_v2/test_step16_mission_advance_security.py
"""

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ENGINE = "http://127.0.0.1:8000"
ROUTE = "/api/v2/chat/mission/advance"


def load_keys() -> dict:
    path = Path(__file__).resolve().parents[2] / ".env.keys"
    if not path.exists():
        print("  .env.keys not found. Run mint_keys.py first.")
        sys.exit(1)
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            a, _, b = line.partition("=")
            out[a.strip()] = b.strip()
    return out


K = load_keys()
PK_DEMO = K["CV3_PUBLISHABLE_CONN_DEMO"]
PK_KETTLE = K["CV3_PUBLISHABLE_CONN_KETTLE"]

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


def req(method, path, *, key=None, body=None, jar=None, raw=None):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    r = urllib.request.Request(ENGINE + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    if key:
        r.add_header("Authorization", f"Bearer {key}")
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar)) if jar is not None \
        else urllib.request.build_opener()
    try:
        with opener.open(r, timeout=60) as resp:
            text = resp.read().decode("utf-8", "replace")
            return resp.status, (json.loads(text) if text else {}), text
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(text), text
        except Exception:  # noqa: BLE001
            return e.code, {}, text


def advance(key, jar, conn, sid, cid):
    return req("POST", ROUTE, key=key, jar=jar,
               body={"connection_id": conn, "session_id": sid, "cart_id": cid})


def chat(jar, sid, cid, message):
    st, body, _ = req("POST", "/api/v2/chat", key=PK_DEMO, jar=jar, body={
        "connection_id": "conn_demo", "session_id": sid, "message": message, "cart_id": cid})
    return st, body


def new_cart(conn, key, jar):
    st, body, _ = req("POST", f"/api/shop/{conn}/cart", key=key, jar=jar)
    assert st == 200, st
    return body["cart_id"]


def cart_lines(conn, key, jar, cid):
    st, body, _ = req("GET", f"/api/shop/{conn}/cart/{cid}", key=key, jar=jar)
    return st, [(ln["title"], ln["quantity"]) for ln in body.get("lines", [])]


MISSION = "I want running shoes, socks and something for energy"

print("1. Registration")
st, schema, _ = req("GET", "/openapi.json")
post = ((schema.get("paths") or {}).get(ROUTE) or {}).get("post")
check("the route is registered for POST in the served OpenAPI schema", st == 200 and post is not None)
check("and only for POST (no GET/PUT/DELETE on it)", set((schema.get("paths") or {}).get(ROUTE, {}).keys()) == {"post"})

jar_a, jar_b = http.cookiejar.CookieJar(), http.cookiejar.CookieJar()
sid_a = f"s16_{uuid.uuid4().hex[:10]}"
cart_a = new_cart("conn_demo", PK_DEMO, jar_a)
st, first = chat(jar_a, sid_a, cart_a, MISSION)
check("setup: visitor A starts a guided mission", st == 200 and "start with" in first.get("reply", "").lower())
sid_b = f"s16_{uuid.uuid4().hex[:10]}"
cart_b = new_cart("conn_demo", PK_DEMO, jar_b)
chat(jar_b, sid_b, cart_b, "show me socks")  # B owns a session and a cart of their own
jar_k = http.cookiejar.CookieJar()
cart_k = new_cart("conn_kettle", PK_KETTLE, jar_k)

print("\n2. Authentication")
body_a = {"connection_id": "conn_demo", "session_id": sid_a, "cart_id": cart_a}
st, _, _ = req("POST", ROUTE, jar=jar_a, body=body_a)
check("refused without a key (401)", st == 401)
st, _, _ = req("POST", ROUTE, key="pk_garbage_not_a_real_key", jar=jar_a, body=body_a)
check("refused with a garbage key (401)", st == 401)
st, _, _ = req("POST", ROUTE, key=PK_KETTLE, jar=jar_a, body=body_a)
check("refused with another merchant's key for conn_demo", st in (401, 404))
st, _, _ = advance(PK_DEMO, jar_a, "conn_nope", sid_a, cart_a)
check("refused for an unknown connection id", st in (401, 404))
st, _, _ = advance(PK_DEMO, jar_a, "conn_kettle", sid_a, cart_a)
check("refused when the demo key names the Kettle connection", st in (401, 404))

print("\n3. Request validation")
for label, raw in (
    ("empty body", b"{}"),
    ("not JSON", b"not json"),
    ("missing cart_id", json.dumps({"connection_id": "conn_demo", "session_id": sid_a}).encode()),
    ("cart_id of the wrong type", json.dumps({"connection_id": "conn_demo", "session_id": sid_a, "cart_id": 123}).encode()),
    ("a list instead of an object", b"[1,2,3]"),
):
    st, _, text = req("POST", ROUTE, key=PK_DEMO, jar=jar_a, raw=raw)
    check(f"{label}: refused as a client error, not a server fault ({st})", 400 <= st < 500)
    check(f"{label}: no stack trace or exception text in the body", "Traceback" not in text and "Exception" not in text)

print("\n4. Ownership and isolation")
st, body, text = advance(PK_DEMO, jar_b, "conn_demo", sid_a, cart_a)
check("another visitor (same publishable key) is refused A's session+cart (404)", st == 404)
check("the refusal reveals nothing about A's mission or cart",
      all(w not in text for w in ("Trailblazer", "socks", "Confirmed", "INR", sid_a, cart_a)))
st, _, _ = advance(PK_DEMO, jar_b, "conn_demo", sid_b, cart_a)
check("B's OWN session with A's cart is refused", st == 404)
st, _, _ = advance(PK_DEMO, jar_b, "conn_demo", sid_a, cart_b)
check("A's session with B's own cart is refused", st == 404)
st, _, _ = advance(PK_DEMO, jar_a, "conn_demo", sid_b, cart_a)
check("A (owner of the cart) with B's session is refused", st == 404)
st, _, _ = advance(PK_DEMO, jar_a, "conn_demo", sid_a, cart_b)
check("A (owner of the session) with B's cart is refused", st == 404)
st, _, _ = advance(PK_DEMO, jar_a, "conn_demo", sid_a, cart_k)
check("a Kettle cart id on the Northfield connection is refused", st == 404)
st, _, _ = advance(PK_KETTLE, jar_k, "conn_kettle", sid_a, cart_k)
check("Kettle key + Kettle cart but Northfield's session id is refused", st == 404)
st, _, _ = advance(PK_KETTLE, jar_k, "conn_kettle", sid_a, cart_a)
check("Kettle key claiming Northfield's session+cart on Kettle is refused", st == 404)

print("\n5. No first-touch claim")
fresh_sid = f"s16_fresh_{uuid.uuid4().hex[:10]}"
st, _, _ = advance(PK_DEMO, jar_a, "conn_demo", fresh_sid, cart_a)
check("an unknown session id is refused (the call cannot originate a session)", st == 404)
st, reply = chat(jar_b, fresh_sid, cart_b, "show me socks")
check("...and it was NOT claimed by that call: another visitor's first chat turn still gets it", st == 200)
st, _, _ = advance(PK_DEMO, jar_a, "conn_demo", fresh_sid, cart_a)
check("...and A is then (correctly) refused that now-B-owned session", st == 404)
st, _, _ = advance(PK_DEMO, jar_b, "conn_demo", sid_b, f"c_unclaimed_{uuid.uuid4().hex[:10]}")
check("an unknown cart id is refused (the call cannot originate a cart)", st == 404)

print("\n6. Owner behaviour, replay and no cart mutation")
st, nothing, _ = advance(PK_DEMO, jar_a, "conn_demo", sid_a, cart_a)
check("owner, nothing in the cart yet: 200 and no reply", st == 200 and nothing.get("reply") is None and nothing.get("products") == [])
st, lines0 = cart_lines("conn_demo", PK_DEMO, jar_a, cart_a)
check("setup: the owner's cart is empty", st == 200 and lines0 == [])

# the card-tap path: the cart gets a shoe from a route other than a chat turn
st, added, _ = req("POST", f"/api/shop/conn_demo/cart/{cart_a}/lines", key=PK_DEMO, jar=jar_a,
                   body={"product_id": "P1001", "variant_id": "P1001-7", "quantity": 1})
check("setup: a shoe is added through the shop route (as a card tap would)", st == 200)
st, lines1 = cart_lines("conn_demo", PK_DEMO, jar_a, cart_a)

# an attacker's refused call must not consume the owner's pending confirmation
advance(PK_DEMO, jar_b, "conn_demo", sid_a, cart_a)
st, out, _ = advance(PK_DEMO, jar_a, "conn_demo", sid_a, cart_a)
reply = out.get("reply") or ""
check("owner: 200 with the cart confirmation and the next stage", st == 200 and "Confirmed in your cart" in reply and "socks" in reply.lower())
check("the next stage's products are returned", any("Sock" in p.get("title", "") for p in out.get("products", [])))
st, lines2 = cart_lines("conn_demo", PK_DEMO, jar_a, cart_a)
check("the advance did not change the cart (identical before/after)", lines1 == lines2 and len(lines2) == 1 and lines2[0][1] == 1)
st, again, _ = advance(PK_DEMO, jar_a, "conn_demo", sid_a, cart_a)
check("replay: 200 and nothing (idempotent)", st == 200 and again.get("reply") is None)
st, again2, _ = advance(PK_DEMO, jar_a, "conn_demo", sid_a, cart_a)
check("second replay: still nothing", st == 200 and again2.get("reply") is None)
st, lines3 = cart_lines("conn_demo", PK_DEMO, jar_a, cart_a)
check("replays did not change the cart", lines3 == lines2)

# a reply is only ever about the owner's own mission: B has no mission
st, b_adv, _ = advance(PK_DEMO, jar_b, "conn_demo", sid_b, cart_b)
check("a visitor with no mission gets 200 and nothing", st == 200 and b_adv.get("reply") is None)

print()
print(f"  {failures} FAILURE(S)" if failures else "  all mission-advance security checks passed")
sys.exit(1 if failures else 0)
