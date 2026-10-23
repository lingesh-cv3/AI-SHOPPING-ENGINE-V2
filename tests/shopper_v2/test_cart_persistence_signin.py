"""A signed-in shopper's cart survives sign-out / sign-in (HTTP, against the RUNNING isolated stack).

User-reported (2026-10-21 manual test): sign in, add a product, sign out, sign back in -> the cart is empty.

Root cause (CV3 state layer, not the platform): `accounts._finish` let the cart the browser tab was holding
at sign-in ALWAYS replace the cart remembered for the account ("what they are holding now wins"). After a
sign-out the tab cannot read the shopper's own cart (it is owned by the account), so the storefront starts a
fresh EMPTY guest cart; signing back in then sent that empty cart as `guest_cart` and silently re-pointed the
account at it, abandoning the cart that held the product.

Properties asserted:
  restore        same account, new visitor/tab with an empty guest cart -> the original cart, product intact
  new session    same account, no guest cart at all -> the original cart
  guest wins     a guest cart that really holds something still replaces the remembered one (unchanged policy)
  no duplicate   restoring/refreshing changes no quantities
  isolation      another shopper, another merchant: the cart is unreachable; `guest_cart` cannot be used to
                 adopt a cart the caller does not hold

Needs the engine (8000), Northfield (8001), Kettle (8002) and .env.keys, on an isolated database.

    python tests/shopper_v2/test_cart_persistence_signin.py
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
OTHER = "conn_kettle"
failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def load_keys():
    path = Path(__file__).resolve().parents[2] / ".env.keys"
    if not path.exists():
        print("  .env.keys not found. Run mint_keys.py first.")
        sys.exit(1)
    out = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.strip().startswith("#"):
            a, _, b = line.strip().partition("=")
            out[a.strip()] = b.strip().strip('"').strip("'")
    return out


KEYS = load_keys()
PK = {CONN: KEYS["CV3_PUBLISHABLE_CONN_DEMO"], OTHER: KEYS["CV3_PUBLISHABLE_CONN_KETTLE"]}


class Tab:
    """One browser tab: its own cookie jar (visitor cookie + shopper cookie), like a fresh sessionStorage."""

    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, body=None, conn=CONN):
        r = urllib.request.Request(ENGINE + path, data=json.dumps(body).encode() if body is not None else None, method=method)
        r.add_header("Content-Type", "application/json")
        r.add_header("Authorization", f"Bearer {PK[conn]}")
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

    def new_cart(self, conn=CONN):
        st, c = self.call("POST", f"/api/shop/{conn}/cart", conn=conn)
        assert st == 200, (st, c)
        return c["cart_id"]

    def add(self, cart_id, product_id="P3002", conn=CONN):
        return self.call("POST", f"/api/shop/{conn}/cart/{cart_id}/lines", {"product_id": product_id, "quantity": 1}, conn=conn)

    def lines(self, cart_id, conn=CONN):
        st, c = self.call("GET", f"/api/shop/{conn}/cart/{cart_id}", conn=conn)
        return st, [(l.get("product_id"), l.get("quantity")) for l in c.get("lines", [])]

    def sign_up(self, username, guest_cart=None, conn=CONN):
        return self.call("POST", "/api/account/signup", {
            "connection_id": conn, "username": username, "password": "correct-horse-9", "email": f"{username}@example.test",
            "guest_cart": guest_cart}, conn=conn)

    def sign_in(self, username, guest_cart=None, conn=CONN):
        return self.call("POST", "/api/account/signin", {
            "connection_id": conn, "username": username, "password": "correct-horse-9", "guest_cart": guest_cart}, conn=conn)

    def sign_out(self):
        return self.call("POST", "/api/account/signout", {})


def main():
    user = f"cp_{uuid.uuid4().hex[:10]}"
    print("1. Sign up with a cart that holds a product, sign out")
    tab1 = Tab()
    cart1 = tab1.new_cart()
    st, c = tab1.add(cart1)
    check("setup: the product is in the cart", st == 200 and len(c.get("lines", [])) == 1, (st, c))
    st, acct = tab1.sign_up(user, guest_cart=cart1)
    check("sign-up remembers that cart for the account", st == 200 and acct.get("cart_id") == cart1, (st, acct.get("cart_id")))
    tab1.sign_out()

    print("\n2. Sign back in from a NEW tab holding an EMPTY guest cart (what the storefront does after sign-out)")
    tab2 = Tab()
    guest_empty = tab2.new_cart()
    st, acct = tab2.sign_in(user, guest_cart=guest_empty)
    check("sign-in succeeds", st == 200, (st, acct))
    check("the account's ORIGINAL cart is restored, not the empty guest cart", acct.get("cart_id") == cart1, acct.get("cart_id"))
    st, lines = tab2.lines(cart1)
    check("the product is still in it, quantity one", st == 200 and lines == [("P3002", 1)], (st, lines))
    st, me = tab2.call("GET", f"/api/account/me?connection_id={CONN}")
    check("/me also hands back the original cart", me.get("signed_in") and me.get("cart_id") == cart1, me.get("cart_id"))
    tab2.sign_out()

    print("\n3. A new session with no guest cart at all")
    tab3 = Tab()
    st, acct = tab3.sign_in(user, guest_cart=None)
    check("the original cart again", acct.get("cart_id") == cart1, acct.get("cart_id"))
    for _ in range(2):
        tab3.call("GET", f"/api/account/me?connection_id={CONN}")
    st, lines = tab3.lines(cart1)
    check("refresh/restore never duplicates the line", lines == [("P3002", 1)], lines)
    tab3.sign_out()

    print("\n4. A guest cart that really HOLDS something still wins (unchanged policy: a fresh choice is not discarded)")
    tab4 = Tab()
    guest_full = tab4.new_cart()
    tab4.add(guest_full, "P3002")
    st, acct = tab4.sign_in(user, guest_cart=guest_full)
    check("the non-empty guest cart becomes the account's cart", acct.get("cart_id") == guest_full, acct.get("cart_id"))
    st, lines = tab4.lines(guest_full)
    check("and keeps its line", lines == [("P3002", 1)], lines)
    st, lines1 = tab4.lines(cart1)
    check("the older cart is left on the platform, not destroyed", st in (200, 403, 404) and (st != 200 or lines1 == [("P3002", 1)]), (st, lines1))
    tab4.sign_out()

    print("\n5. Isolation: another shopper, another merchant")
    other_user = f"cp_{uuid.uuid4().hex[:10]}"
    tab5 = Tab()
    own = tab5.new_cart()
    st, acct = tab5.sign_up(other_user, guest_cart=own)
    st, lines = tab5.lines(guest_full)
    check("another signed-in shopper cannot read the first shopper's cart", st in (403, 404), (st, lines))
    st, acct2 = tab5.sign_in(other_user, guest_cart=guest_full)
    check("naming someone else's cart as guest_cart does not adopt it", acct2.get("cart_id") != guest_full, acct2.get("cart_id"))
    tab6 = Tab()
    st, c = tab6.call("GET", f"/api/shop/{OTHER}/cart/{guest_full}", conn=OTHER)
    check("another merchant's key cannot reach the cart", st in (403, 404), st)
    st, acct3 = tab6.sign_in(user, guest_cart=None, conn=OTHER)
    check("the account does not exist at another merchant (accounts are per merchant)", st == 401, st)

    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


main()
