"""The add contract, end to end over HTTP, model-free.

Step 17 made EVERY add a proposal; Step 25 deliberately narrowed that (the user wants "add it to the cart" to just
add it). Current contract:

    discovery and recommendations never change a cart and never create a proposal;
    an EXPLICIT add command ("add Trailblazer Running Shoe", "add both") executes: a product that needs a size asks
    for it first and adds the shopper's own choice, a size-free product is added, a repeat is "already in your
    cart" (quantity stays one) - and nothing is left pending;
    anything that merely sounds like wanting it ("I'll take this") is still a PROPOSAL the shopper approves:
    nothing is added, rejecting leaves the cart unchanged, approving adds exactly one valid line, a sized product
    asks for its size at approval time.

(The full explicit-add matrix - ambiguity, advice questions, negations, platform failure, concurrency - is in
test_explicit_add_executes.py.)

Probes the RUNNING engine (isolated DB, empty provider keys) through the same
routes the storefront uses: /api/v2/chat, /api/chat/proposals (+ accept/reject),
/api/chat/act (the shopper's own tap on a size button), /api/shop/.../cart.
Needs the engine (8000), Northfield (8001), Kettle (8002) and .env.keys.

    python tests/shopper_v2/test_step17_add_contract.py
"""

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ENGINE = "http://127.0.0.1:8000"
CONN = "conn_demo"


def load_keys():
    path = Path(__file__).resolve().parents[2] / ".env.keys"
    if not path.exists():
        print("  .env.keys not found. Run mint_keys.py first.")
        sys.exit(1)
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            a, _, b = line.strip().partition("=")
            out[a.strip()] = b.strip()
    return out


PK = load_keys()["CV3_PUBLISHABLE_CONN_DEMO"]
failures = 0


def check(label, ok):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class Shopper:
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.sid = f"s17_{uuid.uuid4().hex[:10]}"
        st, cart = self.call("POST", f"/api/shop/{CONN}/cart")
        self.cid = cart["cart_id"]

    def call(self, method, path, body=None):
        r = urllib.request.Request(ENGINE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
        r.add_header("Content-Type", "application/json")
        r.add_header("Authorization", f"Bearer {PK}")
        try:
            with self.opener.open(r, timeout=60) as resp:
                text = resp.read().decode("utf-8", "replace")
                return resp.status, (json.loads(text) if text else {})
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            try:
                return e.code, json.loads(text)
            except Exception:  # noqa: BLE001
                return e.code, {}

    def chat(self, message, variant=None):
        st, r = self.call("POST", "/api/v2/chat", {
            "connection_id": CONN, "session_id": self.sid, "message": message,
            "cart_id": self.cid, "selected_variant_id": variant})
        assert st == 200, (st, message)
        return r

    def cart(self):
        st, c = self.call("GET", f"/api/shop/{CONN}/cart/{self.cid}")
        return [(ln["title"], ln["quantity"], ln.get("variant_id")) for ln in c.get("lines", [])]

    def pending(self):
        st, r = self.call("GET", f"/api/chat/proposals?connection_id={CONN}&session_id={self.sid}&cart_id={self.cid}")
        return r.get("proposals", []) if st == 200 else None

    def body(self):
        return {"connection_id": CONN, "session_id": self.sid, "cart_id": self.cid}

    def accept(self, pid):
        return self.call("POST", f"/api/chat/proposals/{pid}/accept", self.body())

    def reject(self, pid):
        return self.call("POST", f"/api/chat/proposals/{pid}/reject", self.body())

    def tap(self, product_id, variant_id, said="Add that"):
        return self.call("POST", "/api/chat/act", {**self.body(), "product_id": product_id, "variant_id": variant_id, "said": said})


print("1. Discovery and recommendations have no side effects")
s = Shopper()
for msg in ("show me running shoes", "I need socks", "what do you recommend?", "recommend one", "compare both"):
    r = s.chat(msg)
    check(f"{msg!r}: no proposal reference in the reply or trace",
          "proposal" not in r["reply"].lower() and not (r.get("trace") or {}).get("cart_proposal_id"))
check("after all of that the cart is empty", s.cart() == [])
check("and there are no pending proposals", s.pending() == [])

print("\n2. An explicit add of a sized product asks for the size, then adds the shopper's own choice")
s = Shopper()
r = s.chat("add Trailblazer Running Shoe")
opts = ((r["trace"].get("fulfillment_pending") or {}).get("options") or [])
check("it asks for the size and adds nothing yet", "size" in r["reply"].lower() and len(opts) >= 2 and s.cart() == [])
check("no proposal is created for an explicit command", s.pending() == [] and not r["trace"].get("cart_proposal_id"))
r = s.chat("size", variant=opts[0]["variant_id"])
cart = s.cart()
check("the shopper's size is added: exactly one line, quantity one, of that variant",
      len(cart) == 1 and cart[0][1] == 1 and cart[0][2] == opts[0]["variant_id"] and "Trailblazer" in cart[0][0])
check("the reply reports the add and the storefront is told the cart changed",
      "added" in r["reply"].lower() and r["trace"].get("cart_changed") is True)
check("nothing is left pending", s.pending() == [])
r2 = s.chat("add Trailblazer Running Shoe")
check("asking again does not add a second unit", s.cart() == cart and "already" in r2["reply"].lower())

print("\n3. An explicit add of a size-free product is added directly")
s = Shopper()
r = s.chat("add Performance Socks Three Pack")
cart = s.cart()
check("one line, quantity one", len(cart) == 1 and cart[0][1] == 1 and "Socks" in cart[0][0])
check("reported as added, nothing pending", "added" in r["reply"].lower() and s.pending() == [])

print("\n4. Something that merely sounds like wanting it stays a proposal (reject / approve)")
s = Shopper()
s.chat("I need socks")
r = s.chat("I'll take this")
pend = s.pending()
check("'I'll take this' with ONE product in view proposes it - nothing is added",
      len(pend) == 1 and "Socks" in r["reply"] and s.cart() == [])
pid = pend[0]["proposal_id"]
st, out = s.reject(pid)
check("reject succeeds", st == 200 and out.get("state") == "REJECTED")
check("no pending proposal remains and the cart is unchanged (empty)", s.pending() == [] and s.cart() == [])
st, out = s.accept(pid)
check("a rejected proposal can no longer be accepted", not (st == 200 and out.get("cart_changed")) and s.cart() == [])
s = Shopper()
s.chat("I need socks")
s.chat("I'll take this")
pend = s.pending()
st, out = s.accept(pend[0]["proposal_id"])
cart = s.cart()
check("approval adds exactly one line, quantity one", st == 200 and len(cart) == 1 and cart[0][1] == 1 and "Socks" in cart[0][0])
st, again = s.accept(pend[0]["proposal_id"])
check("approving the same proposal again does not add a second unit", s.cart() == cart)
check("the proposal is no longer pending", s.pending() == [])

print("\n5. A proposal for a sized product: approval asks for the size, the shopper's tap adds one valid line")
s = Shopper()
s.chat("show me the trailblazer")
s.chat("I'll take it")
pend = s.pending()
check("a proposal exists for the Trailblazer", len(pend) == 1 and pend[0]["product_id"] == "P1001")
st, out = s.accept(pend[0]["proposal_id"])
choices = out.get("choices") or []
check("approval of a sized product returns the real size choices and adds nothing yet", len(choices) >= 2 and s.cart() == [])
chosen = choices[0]
st, out = s.tap("P1001", chosen["variant_id"], f"The {chosen['label']}, for the Trailblazer Running Shoe")
cart = s.cart()
check("the size tap adds exactly one line of the chosen variant",
      st == 200 and len(cart) == 1 and cart[0][1] == 1 and cart[0][2] == chosen["variant_id"])

print("\n6. 'add both' (explicit): each product's size question first, then both are added - no proposals")
s = Shopper()
s.chat("show me running shoes under 5000")
r = s.chat("add both")
check("it asks the first product's size", "size" in r["reply"].lower() and s.cart() == [] and s.pending() == [])
opts = ((r["trace"].get("fulfillment_pending") or {}).get("options") or [])
r = s.chat("size", variant=opts[0]["variant_id"])
opts = ((r["trace"].get("fulfillment_pending") or {}).get("options") or [])
check("then the second product's size; the first is already in the cart", bool(opts) and len(s.cart()) == 1)
r = s.chat("size", variant=opts[0]["variant_id"])
cart = s.cart()
check("after both choices both products are in the cart, one line each, and nothing is pending",
      len(cart) == 2 and all(q == 1 for _, q, _ in cart) and s.pending() == [] and r["trace"].get("cart_changed") is True)
check("the two lines are two different products", len({title for title, _, _ in cart}) == 2)

print("\n7. The tap path is still the shopper's own explicit action")
s = Shopper()
st, out = s.tap("P3002", None, "Add the Performance Socks Three Pack")
check("a product-card tap adds directly (a tap is trusted, unchanged)", st == 200 and len(s.cart()) == 1)

print()
print(f"  {failures} FAILURE(S)" if failures else "  all add-contract checks passed")
sys.exit(1 if failures else 0)
