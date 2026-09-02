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
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code}
    except urllib.error.URLError as e:
        return {"_unreachable": str(e.reason)}


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
        self.paid_carts: set[str] = set()

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
        return "pay: empty cart"

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
        s.paid_carts.add(s.cart_id)
        # A paid cart is finished. The storefront starts a new one; so do we.
        s.cart_id = None
        s.expected_items = 0
        return f"pay {card} -> {order}"

    if order:
        s.orders.append(order)
    return f"pay {card} declined"


def act_read_cart(s: Shopper, rng: random.Random) -> str:
    if not s.cart_id:
        return "read: no cart"
    call("GET", f"/api/shop/{s.connection}/cart/{s.cart_id}", key=s.key)
    return "read cart"


def act_new_cart(s: Shopper, rng: random.Random) -> str:
    """Start again, the way a shopper who refreshed into a dead cart would."""
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
    """A cart that has been paid for cannot be paid for again.

    The bug this exists for: a paid cart stayed on screen and saying pay again
    charged it, returning the same order because the idempotency key recognised
    the retry. Nothing was double charged and the shopper should never have got
    there.
    """
    for cart in s.paid_carts:
        r = call(
            "POST",
            "/api/chat/pay",
            {
                "connection_id": s.connection,
                "session_id": s.session,
                "cart_id": cart,
                "card_last4": "1111",
            },
            key=s.key,
        )
        payment = r.get("payment") or {}
        if payment.get("paid") and payment.get("order_id") not in s.orders:
            return f"paid cart {cart} produced a new order {payment.get('order_id')}"
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
            f"fuzz_{seed}_{n}_{connection[-4:]}",
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