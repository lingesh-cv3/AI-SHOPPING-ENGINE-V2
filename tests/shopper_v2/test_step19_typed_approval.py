"""Typed approval of a pending proposal ("yes", "approve it", "add it"), over HTTP.

A typed confirmation is the shopper's own approval ONLY when the whole message is
a confirmation and it refers unambiguously to the ONE proposal waiting on their
cart. It runs the same accept core the Accept button runs (so risk, stock/price
re-checks, the atomic claim and the variant requirement are unchanged), never a
parallel path:

    positive            "yes" / "approve it" / "add it" over one pending proposal adds
                        exactly one line, and the proposal is no longer pending
    duplicate           a replayed "yes" finds nothing pending and adds nothing
    no pending          a bare "yes" never changes the cart and creates nothing
    ambiguous           several pending -> a question; NOTHING is approved
    missing variant     a sized product returns the real size buttons; the shopper's
                        own size tap then adds exactly one line of that variant
    rejected            after rejection "yes" has nothing to approve
    cross-shopper       another visitor can neither approve nor consume this
                        shopper's proposal

Probes the RUNNING engine (isolated DB, empty provider keys). Needs the engine
(8000), Northfield (8001), Kettle (8002) and .env.keys. The stale-proposal case is
in test_step19_conversation_gaps.py (it needs an aged row in its own database).

    python tests/shopper_v2/test_step19_typed_approval.py
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
    def __init__(self, jar=None):
        self.jar = jar or http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.sid = f"s19_{uuid.uuid4().hex[:10]}"
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

    def chat(self, message, sid=None, cid=None):
        st, r = self.call("POST", "/api/v2/chat", {
            "connection_id": CONN, "session_id": sid or self.sid, "message": message, "cart_id": cid or self.cid})
        return st, r

    def say(self, message):
        st, r = self.chat(message)
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

    def reject(self, pid):
        return self.call("POST", f"/api/chat/proposals/{pid}/reject", self.body())

    def accept(self, pid):
        return self.call("POST", f"/api/chat/proposals/{pid}/accept", self.body())

    def tap(self, product_id, variant_id):
        return self.call("POST", "/api/chat/act", {**self.body(), "product_id": product_id, "variant_id": variant_id, "said": "size"})


# Step 25: an explicit "add X" now executes, so a PENDING proposal (the fixture these approval behaviours need) comes from a non-explicit intent ("I'll take the X"), which is still only a proposal.
def fresh_with_socks_proposal():
    s = Shopper()
    s.say("I'll take the Performance Socks Three Pack")
    assert len(s.pending()) == 1
    return s


print("1. Positive: the whole message is a confirmation of the one pending proposal")
for phrase in ("yes", "approve it", "add it", "go ahead", "Yes, please."):
    s = fresh_with_socks_proposal()
    check(f"{phrase!r}: nothing in the cart before it", s.cart() == [])
    r = s.say(phrase)
    cart = s.cart()
    check(f"{phrase!r}: exactly one line of the proposed product, quantity one",
          len(cart) == 1 and cart[0][1] == 1 and "Socks" in cart[0][0])
    check(f"{phrase!r}: the proposal is no longer pending", s.pending() == [])
    check(f"{phrase!r}: the reply is not a processing failure", "couldn't process" not in r["reply"].lower())

print("\n2. A replayed confirmation cannot add a second line")
s = fresh_with_socks_proposal()
s.say("yes")
before = s.cart()
r = s.say("yes")
check("the replay finds nothing waiting", "nothing waiting" in r["reply"].lower())
r = s.say("approve it")
check("a different confirmation phrase is also a no-op", s.cart() == before and len(before) == 1 and before[0][1] == 1)

print("\n3. A bare 'yes' with no pending proposal never mutates anything")
s = Shopper()
for phrase in ("yes", "ok", "approve it", "go ahead"):
    r = s.say(phrase)
    check(f"{phrase!r} with nothing pending: told so, cart empty, nothing created",
          "nothing waiting" in r["reply"].lower() and s.cart() == [] and s.pending() == [])
s.say("show me socks")
r = s.say("yes")
check("after plain discovery, 'yes' still adds nothing and proposes nothing", s.cart() == [] and s.pending() == [])

print("\n4. Several pending: ask, approve nothing")
s = Shopper()
s.say("I'll take the Performance Socks Three Pack")
s.say("I'll take the Energy Gel Box of 12")
pend = s.pending()
check("setup: two pending proposals", len(pend) == 2)
r = s.say("yes")
check("'yes' lists them and asks which", "2 suggestions waiting" in r["reply"] and "Which one" in r["reply"])
check("nothing was approved: cart empty, both still pending", s.cart() == [] and len(s.pending()) == 2)
st, out = s.accept(pend[0]["proposal_id"])
check("the shopper can still approve ONE explicitly (Accept)", st == 200 and len(s.cart()) == 1 and len(s.pending()) == 1)
r = s.say("yes")
check("with exactly one left, 'yes' now approves that one", len(s.cart()) == 2 and s.pending() == [])

print("\n5. Missing variant: the size choice stays the shopper's own")
s = Shopper()
s.say("I'll take the Trailblazer Running Shoe")
r = s.say("yes")
choices = (r["trace"].get("accept_choices") or [])
check("the reply presents the real size buttons", len(choices) >= 2 and "more than one option" in r["reply"].lower())
check("nothing is in the cart until the size is chosen", s.cart() == [])
check("the proposal is retired (cannot be confirmed into a second add)", s.pending() == [])
r2 = s.say("yes")
check("a further 'yes' does not pick a size for them", s.cart() == [] and "nothing waiting" in r2["reply"].lower())
chosen = choices[0]
st, out = s.tap(chosen["product_id"], chosen["variant_id"])
cart = s.cart()
check("the shopper's own size tap adds exactly one line of the chosen variant",
      st == 200 and len(cart) == 1 and cart[0][1] == 1 and cart[0][2] == chosen["variant_id"])

print("\n6. Rejected proposals cannot be approved by typing")
s = fresh_with_socks_proposal()
pid = s.pending()[0]["proposal_id"]
st, out = s.reject(pid)
check("rejection succeeds", st == 200 and out.get("state") == "REJECTED")
r = s.say("yes")
check("'yes' after rejection: nothing waiting, cart unchanged", "nothing waiting" in r["reply"].lower() and s.cart() == [])

print("\n7. Cross-shopper")
a = fresh_with_socks_proposal()
b = Shopper()
st, r = b.chat("yes", sid=a.sid, cid=a.cid)
check("another visitor cannot reach A's session/cart by typing 'yes' there (404)", st == 404)
check("...and A's proposal is still pending and the cart still empty", len(a.pending()) == 1 and a.cart() == [])
rb = b.say("yes")
check("B's own 'yes' (nothing pending for B) changes nothing for either shopper",
      "nothing waiting" in rb["reply"].lower() and b.cart() == [] and a.cart() == [] and len(a.pending()) == 1)
st, out = b.call("POST", f"/api/chat/proposals/{a.pending()[0]['proposal_id']}/accept", {**a.body()})
check("B cannot approve A's proposal through the accept route either", st in (403, 404) and a.cart() == [])
a.say("yes")
check("and A's own 'yes' still works afterwards", len(a.cart()) == 1)

print()
print(f"  {failures} FAILURE(S)" if failures else "  all typed-approval checks passed")
sys.exit(1 if failures else 0)
