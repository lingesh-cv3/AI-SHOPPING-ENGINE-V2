"""Random shopper sequences, checking what must stay true after every step.

healthcheck.py walks one path. Every bug found by hand today came from a shopper
doing something slightly different: adding then removing, switching merchant
mid-visit, paying twice, refreshing at an awkward moment. One path cannot find those.

So this does not walk a path. It picks actions at random and after every one asserts
things that must hold regardless of order:

  the cart the engine reports matches what was added and removed
  a paid cart is never chargeable again
  one merchant's session never contains another's turns
  one merchant's cart id is never valid on the other
  an order from one merchant never resolves on the other

Model-free by design. Adds go through the tap endpoint and payments through the pay
endpoint, neither of which calls the provider - so this can run while rate limited,
which is when a check is most likely to be skipped.

Every run prints its seed. A failure is reproducible with --seed, which matters:
a fuzzer that finds a bug you cannot reproduce has told you almost nothing.
The seed replays the action sequence; the session ids are fresh each run, because
the engine files a conversation under whoever first spoke in it and re-running
would otherwise arrive as a stranger to its own transcript.

    python fuzz.py                  20 sequences of 12 steps
    python fuzz.py --seed 12345     repeat a specific run
    python fuzz.py --sequences 100  longer soak

What it cannot see: anything in the browser. Today's session-orphaning and
cart-after-switch bugs lived in React state against sessionStorage, and no
server-side check would have caught them. Worth knowing before trusting a green run
too far.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import urllib.error
import http.cookiejar
import urllib.request
from pathlib import Path

ENGINE = "http://127.0.0.1:8000"

MERCHANTS = {
    "conn_demo": {
        "products": [
            ("P1001", "P1001-8"),
            ("P1002", "P1002-9"),
            ("P1003", None),
            ("P1005", "P1005-8"),
        ],
        "secret": "CV3_SECRET_CONN_DEMO",
    },
    "conn_kettle": {
        "products": [
            ("KB-ETH-01", "KB-ETH-01::250g whole bean"),
            ("KB-COL-02", "KB-COL-02::250g ground"),
            ("KB-BLD-05", "KB-BLD-05::250g whole bean"),
            ("KB-EQP-10", None),
        ],
        "secret": "CV3_SECRET_CONN_KETTLE",
    },
}


def load_keys() -> dict[str, str]:
    path = Path(".env.keys")
    if not path.exists():
        print("\n  .env.keys not found. Run mint_keys.py first.\n")
        sys.exit(1)
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip()
    return out


KEYS = load_keys()


#: The account the shared browser uses at each merchant, keyed by connection id.
#:
#: The one shopper cookie can hold only one merchant's session - signing in at
#: the second shop overwrites the first. Reaching for a merchant again means
#: signing back in as the same account, so the credentials are kept here rather
#: than minting a fresh account whose shopper_id owns nothing the browser built.
_ACCOUNTS: dict[str, tuple[str, str]] = {}

#: Which merchant's shopper session the one cookie currently holds; None is guest.
#:
#: Tracks the cookie so an action is only signed in/out when needed. Every act is
#: run as the owning identity: a merchant that has an account is acted on as that
#: account - the carts and conversation it claimed are otherwise unreachable the
#: moment the browser switches to the other shop - and one that does not is a
#: guest, so its first basket belongs to the visitor cookie, which every shop and
#: every later account can still reach.
_CURRENT: str | None = None

#: One cookie jar for the whole run, so the suite looks like one browser.
#:
#: The engine now files a cart, a conversation and an order under whichever
#: browser made it, and refuses the rest. Without a jar every request here would
#: arrive as a different stranger and the second call of every pair would be
#: refused - correctly, which is the point.
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def call(method: str, path: str, body: dict | None = None, key: str | None = None):
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
        with _OPENER.open(req, timeout=30) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code}
    except urllib.error.URLError as e:
        return {"_unreachable": str(e.reason)}


def signed_in_for(
    connection: str, *, cart_id: str | None = None, tag: str = "fz"
) -> None:
    """Sign the shared browser in at `connection`, opening its account if needed.

    Checkout now requires a signed-in account with an email, so every pay action
    runs as one. The first call opens the browser's account at this merchant;
    later calls re-sign in as that same account, because the one shopper cookie
    can only hold one merchant's session at a time - signing in at the second
    shop overwrites the first, and the first is only reachable again by signing
    back in.

    When a cart already exists in this tab, pass it as *cart_id* so whichever
    sign-in runs (up or in) adopts it from the visitor cookie to the account.
    Without this a cart built under a different identity stays filed under it and
    every subsequent read or pay returns 404.
    """
    credentials = _ACCOUNTS.get(connection)
    if credentials is None:
        username = f"{tag}_{random.randrange(10**8)}"
        password = "password123"
        call(
            "POST",
            "/api/account/signup",
            {
                "connection_id": connection,
                "username": username,
                "password": password,
                "email": f"{username}@example.com",
                "guest_session": None,
                "guest_cart": cart_id,
            },
            key=KEYS[MERCHANTS[connection]["secret"]],
        )
        _ACCOUNTS[connection] = (username, password)
    else:
        username, password = credentials
        call(
            "POST",
            "/api/account/signin",
            {
                "connection_id": connection,
                "username": username,
                "password": password,
                "guest_session": None,
                "guest_cart": cart_id,
            },
            key=KEYS[MERCHANTS[connection]["secret"]],
        )
    _CURRENT = connection


def sign_out() -> None:
    """Back to a guest. The visitor cookie survives, so nothing is lost."""
    call("POST", "/api/account/signout")
    _CURRENT = None


def switch_to(connection: str) -> None:
    """Act on `connection` as whoever owns its state.

    A merchant with an account is acted on as that account - signing in puts the
    cookie back on it, so the carts and conversation it claimed stay reachable
    however often the browser visits the other shop. A merchant without one is
    acted on as a guest, signing out so its first basket is claimed by the
    visitor cookie, which every account and every shop can still reach.
    """
    if connection in _ACCOUNTS:
        if _CURRENT != connection:
            signed_in_for(connection)
    elif _CURRENT is not None:
        sign_out()


class Shopper:
    """One shopper's state, as we believe it to be.

    Kept alongside what the engine reports so the two can be compared. Believing
    the engine's own answer would make the check tautological.
    """

    def __init__(self, connection: str, session: str, key: str) -> None:
        self.connection = connection
        self.session = session
        self.key = key
        self.cart_id: str | None = None

        #: How many items we believe the cart holds.
        #:
        #: A count rather than a list of what was added. Kettle merges two adds of
        #: the same variant into one line with a quantity of two, so a list of add
        #: actions and a list of cart lines disagree - and item_count follows the
        #: quantity. Counting quantity works on both platforms, which is the point:
        #: two platforms behaving differently is what this engine absorbs, and the
        #: check should absorb it too.
        self.expected_items = 0
        self.orders: list[str] = []

        #: Cart id -> the card that paid it.
        #:
        #: Was a bare set. The platform key was derived from the cart AND the
        #: card, so a retry on the same card was recognised and a retry on a
        #: different one was not - and a set could not tell
        #: check_paid_cart_not_reusable which card had already been used, so it
        #: always retried with "1111" whatever had actually paid. Every paid cart
        #: in this suite is paid with "1111" (the only card act_pay ever succeeds
        #: with), so that check retried the same card on the same card and passed
        #: while a different card bought the basket again. Recording the card
        #: is what lets the check reach for a different one on purpose.
        self.paid_carts: dict[str, str] = {}

    def ensure_cart(self) -> str | None:
        if self.cart_id is None:
            r = call(
                "POST",
                f"/api/shop/{self.connection}/cart",
                key=self.key,
            )
            self.cart_id = r.get("cart_id")
            self.expected_items = 0
        return self.cart_id


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def act_add(s: Shopper, rng: random.Random) -> str:
    switch_to(s.connection)
    cart = s.ensure_cart()
    if not cart:
        return "add: no cart"

    product, variant = rng.choice(MERCHANTS[s.connection]["products"])
    body = {
        "connection_id": s.connection,
        "session_id": s.session,
        "product_id": product,
        "cart_id": cart,
        "said": f"Add {product}",
    }
    if variant:
        body["variant_id"] = variant

    r = call("POST", "/api/chat/act", body, key=s.key)

    if r.get("cart_changed"):
        s.expected_items += 1
        return f"add {product}"

    # Asked which option, or sold out. Neither changed the cart, so neither
    # changes what we expect.
    return f"add {product} (no change)"


def act_remove(s: Shopper, rng: random.Random) -> str:
    switch_to(s.connection)
    if not s.cart_id or s.expected_items == 0:
        return "remove: nothing to remove"

    cart = call("GET", f"/api/shop/{s.connection}/cart/{s.cart_id}", key=s.key)
    lines = cart.get("lines") or []
    if not lines:
        return "remove: cart already empty"

    line = rng.choice(lines)
    r = call(
        "PATCH",
        f"/api/shop/{s.connection}/cart/{s.cart_id}/lines/{line['line_id']}",
        {"quantity": 0},
        key=s.key,
    )

    if "_status" in r:
        # The shop proxy may not expose a quantity update this way; not a failure
        # of the invariant, just an action we cannot take.
        return "remove: not supported by the proxy"

    # The line's own quantity, not one. Removing a line that held two takes two
    # items out, and subtracting one was how the expectation drifted.
    s.expected_items -= int(line.get("quantity", 1))
    return f"remove {line.get('title', '')[:20]} x{line.get('quantity', 1)}"


def act_pay(s: Shopper, rng: random.Random) -> str:
    if not s.cart_id or s.expected_items == 0:
        # The empty branch needs the right identity too: check_cart_matches reads
        # the (empty but existing) cart right after, and with the wrong cookie on
        # it an account-owned cart reads as a stranger's 404.
        switch_to(s.connection)
        return "pay: empty cart"

    signed_in_for(s.connection, cart_id=s.cart_id)
    card = rng.choice(["1111", "1111", "0002"])
    r = call(
        "POST",
        "/api/chat/pay",
        {
            "connection_id": s.connection,
            "session_id": s.session,
            "cart_id": s.cart_id,
            "card_last4": card,
        },
        key=s.key,
    )

    payment = r.get("payment") or {}
    order = payment.get("order_id")

    if payment.get("paid"):
        if order:
            s.orders.append(order)
        # Recorded with the card that paid it, so the invariant check below can
        # deliberately reach for a different one.
        s.paid_carts[s.cart_id] = card
        # A paid cart is finished. The storefront starts a new one; so do we.
        s.cart_id = None
        s.expected_items = 0
        return f"pay {card} -> {order}"

    if order:
        s.orders.append(order)
    return f"pay {card} declined"


def act_read_cart(s: Shopper, rng: random.Random) -> str:
    switch_to(s.connection)
    if not s.cart_id:
        return "read: no cart"
    call("GET", f"/api/shop/{s.connection}/cart/{s.cart_id}", key=s.key)
    return "read cart"


def act_new_cart(s: Shopper, rng: random.Random) -> str:
    """Start again, the way a shopper who refreshed into a dead cart would."""
    switch_to(s.connection)
    s.cart_id = None
    s.expected_items = 0
    s.ensure_cart()
    return "new cart"


ACTIONS = [
    (act_add, 4),
    (act_remove, 2),
    (act_pay, 2),
    (act_read_cart, 1),
    (act_new_cart, 1),
]


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def check_cart_matches(s: Shopper) -> str | None:
    """The engine's cart holds what we added and nothing else."""
    if not s.cart_id:
        return None

    cart = call("GET", f"/api/shop/{s.connection}/cart/{s.cart_id}", key=s.key)
    if "_status" in cart:
        return f"cart {s.cart_id} unreadable ({cart['_status']})"

    reported = cart.get("item_count", 0)
    expected = s.expected_items

    if reported != expected:
        return f"cart holds {reported} item(s), expected {expected}"
    return None


def check_paid_cart_not_reusable(s: Shopper) -> str | None:
    """A cart that has been paid for cannot be paid for again, on any card.

    This used to retry with "1111" regardless of what had paid the cart - and
    "1111" is the only card act_pay ever succeeds with, so every retry was the
    same card retrying itself. The platform key was derived from the cart AND
    the card, so that was recognised as a retry and returned the original order,
    which made this pass throughout a live double-charge bug: a DIFFERENT card
    was a different key, and the same basket bought a second order at the full
    amount. The docstring here used to describe that as the correct outcome.

    So this tries the card that paid it - which now goes through the engine's
    own ledger rather than the platform's key - and a second, genuinely
    different one, which is the case that was never being asked.

    Nothing to check means nobody signs in. This runs after every step, and an
    unconditional sign-in here churned the cookie: the browser holds one shopper
    session, so re-signing-in at the merchant just acted on leaves the previous
    merchant's account unreachable and every cart filed under it a 404.
    """
    if not s.paid_carts:
        return None
    signed_in_for(s.connection)
    for cart, paid_with in s.paid_carts.items():
        other_card = "2222" if paid_with != "2222" else "3333"
        for card in (paid_with, other_card):
            r = call(
                "POST",
                "/api/chat/pay",
                {
                    "connection_id": s.connection,
                    "session_id": s.session,
                    "cart_id": cart,
                    "card_last4": card,
                },
                key=s.key,
            )
            payment = r.get("payment") or {}
            if payment.get("paid") and payment.get("order_id") not in s.orders:
                return (
                    f"paid cart {cart} (originally paid with {paid_with}) "
                    f"produced a new order {payment.get('order_id')} when "
                    f"retried with {card}"
                )
    return None


def check_session_isolation(shoppers: dict[str, Shopper]) -> str | None:
    """One merchant's session never contains another's turns."""
    for connection, s in shoppers.items():
        r = call(
            "GET",
            f"/api/chat/{connection}/{s.session}",
            key=s.key,
        )
        if "_status" in r:
            continue

        other_products = [
            p
            for c, m in MERCHANTS.items()
            if c != connection
            for p, _ in m["products"]
        ]
        text = " ".join(t.get("text", "") for t in (r.get("turns") or []))
        for product in other_products:
            if product in text:
                return f"{connection} session mentions {product}, another merchant's"
    return None


def check_cross_merchant_cart(shoppers: dict[str, Shopper]) -> str | None:
    """A cart id from one merchant is not valid on the other.

    Checked with the correct key for the merchant being asked, so a refusal means
    the cart genuinely is not theirs rather than the key being wrong.
    """
    connections = list(shoppers)
    for i, a in enumerate(connections):
        for b in connections[i + 1 :]:
            cart = shoppers[a].cart_id
            if not cart:
                continue
            r = call(
                "GET",
                f"/api/shop/{b}/cart/{cart}",
                key=shoppers[b].key,
            )
            if "_status" not in r and r.get("cart_id"):
                return f"{a}'s cart {cart} was readable on {b}"
    return None


def check_cross_merchant_order(shoppers: dict[str, Shopper]) -> str | None:
    """An order from one merchant does not resolve on the other."""
    connections = list(shoppers)
    for i, a in enumerate(connections):
        for b in connections[i + 1 :]:
            for order in shoppers[a].orders:
                r = call(
                    "GET",
                    f"/api/shop/{b}/order/{order}",
                    key=shoppers[b].key,
                )
                if "_status" not in r and r.get("order_id"):
                    return f"{a}'s order {order} was readable on {b}"
    return None


# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument("--sequences", type=int, default=20)
parser.add_argument("--steps", type=int, default=12)
parser.add_argument("--seed", type=int, default=None)
args = parser.parse_args()

if "_unreachable" in call("GET", "/health"):
    print("\n  The engine is not answering on port 8000.\n")
    sys.exit(1)

seed = args.seed if args.seed is not None else random.randrange(1, 10**9)
rng = random.Random(seed)

# A per-process nonce in the session ids. The seed makes the action sequence
# reproducible, but the sessions themselves must not be: the engine files a
# conversation under whoever first spoke in it, and re-running the same seed
# (or running after any run that drew the same seed) would then arrive as a
# different stranger and be refused the transcript - correctly, which makes
# the run fail. Uniqueness, not reproducibility, is what a session id needs.
_NONCE = random.randrange(1_000_000_000)

print()
print(f"  seed {seed}   {args.sequences} sequences x {args.steps} steps")
print("  (rerun a failure with --seed)")
print()

#: What each action actually did, so a green run can be checked for reach.
COVERAGE: dict[str, int] = {}

failures: list[tuple[int, str, list[str]]] = []
checks_run = 0

for n in range(args.sequences):
    shoppers = {
        connection: Shopper(
            connection,
            f"fuzz_{_NONCE}_{seed}_{n}_{connection[-4:]}",
            KEYS.get(MERCHANTS[connection]["secret"], ""),
        )
        for connection in MERCHANTS
    }

    log: list[str] = []
    broke = None

    for step in range(args.steps):
        # A shopper switching merchant mid-visit, which is where several of the
        # real bugs lived.
        connection = rng.choice(list(shoppers))
        s = shoppers[connection]

        action = rng.choices(
            [a for a, _ in ACTIONS], weights=[w for _, w in ACTIONS]
        )[0]
        outcome = action(s, rng)
        COVERAGE[outcome.split(" ->")[0].split(" (")[0]] = (
            COVERAGE.get(outcome.split(" ->")[0].split(" (")[0], 0) + 1
        )
        log.append(f"{connection[-6:]}: {outcome}")

        for check in (check_cart_matches, check_paid_cart_not_reusable):
            problem = check(s)
            checks_run += 1
            if problem:
                broke = f"{connection}: {problem}"
                break

        if broke:
            break

        for check in (
            check_session_isolation,
            check_cross_merchant_cart,
            check_cross_merchant_order,
        ):
            problem = check(shoppers)
            checks_run += 1
            if problem:
                broke = problem
                break

        if broke:
            break

    if broke:
        failures.append((n, broke, log))
        print(f"  FAIL  sequence {n}: {broke}")
    else:
        print(f"  ok    sequence {n}")

print()
print(f"  {checks_run} assertions across {args.sequences} sequences")
print()
print("  what actually happened:")
for what, count in sorted(COVERAGE.items(), key=lambda kv: -kv[1]):
    print(f"    {count:4}  {what}")
print()

if failures:
    print(f"  {len(failures)} sequence(s) broke an invariant.")
    print()
    for n, why, log in failures[:3]:
        print(f"  sequence {n}: {why}")
        for line in log:
            print(f"      {line}")
        print()
    print(f"  Reproduce with: python fuzz.py --seed {seed}")
    print()
    sys.exit(1)

print("  Every invariant held.")
print()
print("  Worth remembering what this cannot see: anything in the browser. The")
print("  session and cart bugs found by hand today lived in React state against")
print("  sessionStorage, and no server-side check would have caught them.")
print()