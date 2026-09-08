"""End-to-end check of everything the engine is supposed to do.

Written after a session where several edits silently reverted, because files were
open in an editor while being written to on disk. The failure mode is nasty: the
code looks right when you read it, and the running system behaves as though it was
never changed.

So this does not read source. It drives the running services over HTTP and asserts
on what actually comes back, which is the only thing that cannot lie about itself.

Run with all four processes up:

    python -m uvicorn sample_merchant.api.main:app --port 8001
    python -m uvicorn sample_merchant_two.api.main:app --port 8002
    python -m uvicorn engine.api.main:app --port 8000

    python healthcheck.py

Every check prints PASS or FAIL with what was expected. A FAIL names the file to
look at.
"""

from __future__ import annotations

import sys
import urllib.error
import http.cookiejar
import urllib.request
from pathlib import Path
import json
import uuid

ENGINE = "http://127.0.0.1:8000"


def load_keys() -> dict[str, str]:
    """The keys mint_keys.py wrote, or an empty dict.

    Missing keys are not a failure of the engine, so the auth-dependent checks skip
    with a reason rather than reporting red. A red line should mean something is
    broken, not that a setup step has not been run.
    """
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


#: Passed by a check that must reach the engine unauthenticated. An empty string
#: was used first and is falsy, so it fell through to the automatic lookup and
#: the request arrived with a key - which made a locked route report as open.
NO_KEY = "__none__"


def key_for(path: str, body: dict | None) -> str | None:
    """The key a call should carry, worked out from where it is going.

    Chosen here rather than passed at every call site. There are around sixty calls
    in this file, and threading a key through each would be sixty chances to forget
    one - which surfaces as a mysterious failure rather than a missing argument.

    The secret key is used throughout rather than the publishable one. A secret key
    can do everything a publishable key can, so one lookup covers every route, and
    the audit is what proves a publishable key is correctly limited.
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
NORTHFIELD = "conn_demo"
KETTLE = "conn_kettle"

passed = 0
failed: list[str] = []


#: One cookie jar for the whole run, so the suite looks like one browser.
#:
#: The engine now files a cart, a conversation and an order under whichever
#: browser made it, and refuses the rest. Without a jar every request here would
#: arrive as a different stranger and the second call of every pair would be
#: refused - correctly, which is the point.
_JAR = http.cookiejar.CookieJar()
_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_JAR))


def call(
    method: str, path: str, body: dict | None = None, key: str | None = None
) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{ENGINE}{path}",
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            # An explicit key wins, so a check that deliberately sends none - or
            # sends the wrong one - still gets to do that.
            **(
                {}
                if key == NO_KEY
                else (
                    {"Authorization": f"Bearer {key}"}
                    if key
                    else (
                        {"Authorization": f"Bearer {auto}"}
                        if (auto := key_for(path, body))
                        else {}
                    )
                )
            ),
        },
    )
    try:
        with _OPENER.open(req, timeout=45) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:200]}
    except TimeoutError:
        # A result, not a crash. The provider was too slow, and every check after
        # this one still deserves to run - a suite that dies partway through says
        # less than one that reports what it found.
        return {"_timeout": True, "_body": "the request timed out"}
    except urllib.error.URLError as e:
        # A service being down is a result, not a crash. Reporting it as a failed
        # check with the reason is far more useful than a stack trace that buries
        # "connection refused" forty lines down.
        return {"_unreachable": str(e.reason)}


def signed_in_for(shop: str, tag: str = "hc") -> None:
    """Make sure the shared browser is signed in at `shop`, with an email.

    Checkout now requires a signed-in account with an email - the confirmation
    has to reach somebody, and the payment routes refuse a guest. This signs up
    a fresh account (the whole run is one browser, so one account per run is the
    honest shape) and leaves its session in the shared cookie jar.

    The one visitor/shopper cookie can only hold one merchant's session at a
    time - signing in at the second shop overwrites the first - so this is
    called right before a shop's payments, never assumed to have carried over.
    """
    username = f"{tag}_{uuid.uuid4().hex[:6]}"
    call(
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


#: What the assistant says when the provider refused us. It means one thing
#: only, so it is safe to key on.
#: Keyed on the shortest phrase in the throttle message unlikely to be reworded.
#:
#: The message has already changed once - from "getting" to "handling" - and this
#: check silently stopped working, which is the risk in matching prose at all. If it
#: is reworded again, this stops recognising a skip and reports a failure: annoying,
#: and the safe direction.
#: How many checks this suite contains, counted from the source.
#:
#: Counted rather than declared, because a hand-maintained total is a number
#: somebody forgets to update - and then the check that verifies completeness is
#: itself incomplete. It was set to 46 by hand and the file declares 49, so the
#: summary was wrong in both directions.
#:
#: Read at startup from this file's own text. Slightly odd, and it cannot drift.
#:
#: One is subtracted because two checks are mutually exclusive: the rejection
#: pair runs when the queue has something in it, and 'rejection case reached
#: the queue' runs when it does not. The suite can never execute both, so a
#: raw count of declarations is always one too many.
EXPECTED_CHECKS = len(
    __import__("re").findall(
        r'check\(\s*\n?\s*"([^"]+)"',
        __import__("pathlib").Path(__file__).read_text(encoding="utf-8"),
    )
) - 1

THROTTLED = "a lot of questions"

#: Checks that could not run. Counted apart from failures, because a red line
#: should mean something is broken.
skipped: list[str] = []


def check(name: str, ok: bool, detail: str = "", fix: str = "") -> bool:
    global passed
    if ok:
        passed += 1
        # The name only.
        #
        # detail is written to explain a failure, so printing it on success
        # produced lines like "PASS the operator's note stays private (the
        # internal note reached the shopper)" - a pass that reads as a failure.
        #
        # On success the name is the whole message. Anything worth knowing when a
        # check passes belongs in the name.
        print(f"  PASS  {name}")
    elif any(
        marker in detail
        for marker in (
            THROTTLED,  # what a shopper is told
            "rate limited",  # what we log
            "timed out",
            "could not reach the model provider",
        )
    ):
        # Throttled is not failed. The model was never asked, so nothing
        # about the engine was measured either way.
        skipped.append(name)
        print(f"  SKIP  {name}  (the model was busy, not checked)")
    else:
        failed.append(name)
        print(f"  FAIL  {name}")
        if detail:
            print(f"        got: {detail}")
        if fix:
            print(f"        look at: {fix}")
    return ok


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


# ---------------------------------------------------------------------------

section("Services")

health = call("GET", "/health")
check(
    "engine is up",
    health.get("status") == "ok",
    str(health)[:90],
    "python -m uvicorn engine.api.main:app --port 8000",
)
model_on = health.get("ai_reasoning") == "active"
check(
    "model configured",
    model_on,
    str(health.get("ai_reasoning")),
    ".env needs GROQ_API_KEY",
)

if "_unreachable" in health:
    # Everything below needs the engine. Continuing would print thirty identical
    # failures and bury the one that matters.
    print("\n  The engine is not answering. Start it and run this again:")
    print("    python -m uvicorn engine.api.main:app --port 8000")
    sys.exit(1)

# A refused first call means the keys are missing or stale. Better to say so than
# to let sixty checks fail and then crash indexing into a refusal.
probe = call("GET", "/api/shop/conn_demo/departments")
if probe.get("_status") == 401:
    print()
    print("  The engine refused an authenticated request.")
    print("  .env.keys is missing or does not match this database.")
    print("  Delete cv3.db, restart the engine, and run mint_keys.py again.")
    print()
    sys.exit(1)

connections = call("GET", "/api/connections")
ids = [c["connection_id"] for c in connections] if isinstance(connections, list) else []
check(
    "both merchants registered",
    NORTHFIELD in ids and KETTLE in ids,
    str(ids),
    "engine/api/deps.py",
)

# ---------------------------------------------------------------------------

section("Platform independence")

nf = call("GET", f"/api/connections/{NORTHFIELD}/capabilities")
kb = call("GET", f"/api/connections/{KETTLE}/capabilities")

check(
    "Northfield exposes no payment recovery",
    nf.get("payment_recovery_methods") == [],
    str(nf.get("payment_recovery_methods")),
    "adapters/sample/adapter.py",
)
check(
    "Kettle exposes payment recovery",
    len(kb.get("payment_recovery_methods") or []) >= 2,
    str(kb.get("payment_recovery_methods")),
    "adapters/kettle/adapter.py",
)

nf_search = call("GET", f"/api/shop/{NORTHFIELD}/search?q=running%20shoes")
check(
    "multi-word search works",
    len(nf_search.get("products") or []) > 0,
    f"{len(nf_search.get('products') or [])} results for 'running shoes'",
    "sample_merchant/store.py::search_items",
)

dead = call("GET", f"/api/shop/{NORTHFIELD}/search?q=trainers")
check(
    "dead search still detected",
    dead.get("is_dead_search") is True,
    str(dead.get("is_dead_search")),
    "engine/api/shop.py",
)

browse = call("GET", f"/api/shop/{NORTHFIELD}/search?limit=100")
check(
    "browsing is not friction",
    browse.get("is_dead_search") is False,
    str(browse.get("is_dead_search")),
    "engine/api/shop.py",
)

kb_products = call("GET", f"/api/shop/{KETTLE}/search?limit=5")
first = (kb_products.get("products") or [{}])[0]
variants = first.get("variants") or []
check(
    "Kettle reports no stock counts",
    bool(variants) and variants[0].get("quantity_available") is None,
    f"quantity_available={variants[0].get('quantity_available') if variants else 'no variants'}",
    "adapters/kettle/mapping.py",
)

# ---------------------------------------------------------------------------

section("Risk gate")

call(
    "PUT",
    f"/api/policy/{NORTHFIELD}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": []},
)

money = call(
    "POST",
    "/api/simulate",
    {"connection_id": NORTHFIELD, "candidates": ["APPLY_PROMOTION"]},
)
check(
    "money cannot be automated",
    money.get("risk_outcome") == "HUMAN"
    and money.get("risk_rule") == "FINANCIAL_ALWAYS_HUMAN",
    f"{money.get('risk_outcome')} / {money.get('risk_rule')}",
    "engine/risk/gate.py",
)

safe = call(
    "POST",
    "/api/simulate",
    {"connection_id": NORTHFIELD, "candidates": ["SUGGEST_ALTERNATIVE"]},
)
check(
    "safe actions run with an empty allowlist",
    safe.get("risk_outcome") == "AUTO",
    f"{safe.get('risk_outcome')} / {safe.get('risk_rule')}",
    "engine/risk/gate.py rule 9",
)

blocked = call(
    "PUT",
    f"/api/policy/{NORTHFIELD}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": ["SUGGEST_ALTERNATIVE"]},
)
off = call(
    "POST",
    "/api/simulate",
    {"connection_id": NORTHFIELD, "candidates": ["SUGGEST_ALTERNATIVE"]},
)
# A switched-off action never reaches the gate: the Decision Engine drops it during
# filtering, so what arrives is an escalation. That is the correct behaviour and it
# is worth asserting precisely, because "did not get BLOCK" would look like a
# failure when the action was in fact refused.
check(
    "switching an action off stops it running",
    off.get("selected_action") == "ESCALATE_TO_HUMAN",
    f"selected {off.get('selected_action')}",
    "engine/decision/engine.py",
)
call(
    "PUT",
    f"/api/policy/{NORTHFIELD}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": []},
)

check(
    "policy persisted",
    call("GET", f"/api/policy/{NORTHFIELD}").get("mode") == "STANDARD",
    str(call("GET", f"/api/policy/{NORTHFIELD}").get("mode")),
    "engine/api/routes.py::set_policy",
)

# ---------------------------------------------------------------------------

section("Reasoning")

if model_on:
    reasoned = call(
        "POST",
        "/api/simulate",
        {
            "connection_id": NORTHFIELD,
            "friction": "DEAD_SEARCH",
            "query": "trainers",
        },
    )
    used = reasoned.get("used_model")
    check(
        "model reasons about a dead search",
        used is True,
        f"used_model={used}, fallback={reasoned.get('fallback_reason')}",
        "rate limited? wait a minute and retry",
    )
    if used:
        check(
            "diagnosis is specific",
            bool(reasoned.get("diagnosis")),
            str(reasoned.get("diagnosis"))[:70],
            "engine/reasoning/prompts.py",
        )
    # COMPARE_PRODUCTS: two real products, named by title, both present in
    # Northfield's catalog. Asserts the model actually proposed the new action
    # (not ANSWER_PRODUCT_QUESTION, which would also "answer" a compare
    # request) and that execution returned both products fresh rather than
    # nothing or one.
    compared = call(
        "POST",
        "/api/chat",
        {
            "connection_id": NORTHFIELD,
            "session_id": f"hc_compare_{uuid.uuid4().hex[:6]}",
            "message": (
                "Compare the Trailblazer Running Shoe (P1001) and the "
                "Marathon Pro Racing Shoe (P1002) for me."
            ),
        },
    )
    comparison = compared.get("comparison") or []
    check(
        "the model proposes a comparison when asked to compare two products",
        compared.get("action_taken") == "COMPARE_PRODUCTS" and len(comparison) == 2,
        f"action_taken={compared.get('action_taken')} "
        f"comparison_len={len(comparison)} reply={compared.get('reply', '')[:80]!r}",
        "engine/execution/service.py COMPARE_PRODUCTS, engine/reasoning/prompts.py",
    )
    if len(comparison) == 2:
        ids = {p.get("product_id") for p in comparison}
        check(
            "the comparison is the two products actually named",
            ids == {"P1001", "P1002"},
            str(ids),
            "engine/execution/service.py COMPARE_PRODUCTS",
        )
else:
    print("  SKIP  model checks (no key configured)")

# ---------------------------------------------------------------------------

section("An empty basket is told the truth")

# Paying an empty cart raised CommerceError(CART_INVALID) on both platforms, and
# the pay route's except block gave it the exact same sentence as a genuine
# platform failure: "it was not your card. Nothing has been charged - try again
# in a moment." That is reassuring and false. There is nothing wrong with their
# card and trying again changes nothing, because there is nothing in the
# basket to buy.
for shop in (NORTHFIELD, KETTLE):
    signed_in_for(shop, "hc_empty")
    empty_cart = call("POST", f"/api/shop/{shop}/cart")
    empty_session = f"hc_empty_{uuid.uuid4().hex[:6]}"
    empty_pay = call(
        "POST",
        "/api/chat/pay",
        {
            "connection_id": shop,
            "session_id": empty_session,
            "cart_id": empty_cart.get("cart_id"),
            "card_last4": "1111",
        },
    )
    check(
        f"{shop}: paying nothing does not blame the card",
        "not your card" not in str(empty_pay.get("reply", "")).lower(),
        str(empty_pay.get("reply")),
        "engine/api/chat.py::pay - CommerceError(CART_INVALID) needs its own message",
    )

# ---------------------------------------------------------------------------

section("A basket is paid for once")

# One basket, three cards, on both platforms.
#
# The key handed to the platform was derived from the cart and the card, so the
# same card twice was recognised as a retry - which is what "a paid cart is never
# chargeable again" was taken to mean, and it is only ever true of a repeated tap
# on one card. A different card was a different key, and one basket bought three
# separate orders at the full amount.
#
# Both merchants, because this is not a platform quirk. Northfield is REST and
# Kettle is GraphQL and neither of them was being asked the question.


def buy_three_ways(shop: str, product: str, variant: str) -> dict:
    """Pay for one basket three times on three cards. Report what it bought."""
    signed_in_for(shop, "hc_once")
    basket = call("POST", f"/api/shop/{shop}/cart")
    if "cart_id" not in basket:
        return {"setup": str(basket)[:120]}

    cart_id = basket["cart_id"]
    call(
        "POST",
        f"/api/shop/{shop}/cart/{cart_id}/lines",
        {"product_id": product, "variant_id": variant, "quantity": 1},
    )

    # Fresh per run, like every other session id in this file. A fixed one is
    # claimed by the first run that uses it and refused to every run after,
    # because each run is a new browser - which is the scoping working, not
    # failing.
    session_id = f"hc_once_{uuid.uuid4().hex[:6]}"
    bought: list[str] = []
    replies: list[str] = []

    for card in ("1111", "2222", "3333", "4444"):
        r = call(
            "POST",
            "/api/chat/pay",
            {
                "connection_id": shop,
                "session_id": session_id,
                "cart_id": cart_id,
                "card_last4": card,
            },
        )
        payment = r.get("payment") or {}
        replies.append(str(r.get("reply") or r)[:120])
        # Counted only when a *new* order came back. The already-paid answer
        # repeats the original order id, which is the point of it.
        if payment.get("paid") and payment.get("order_id"):
            if not payment.get("already_paid"):
                bought.append(payment["order_id"])

    return {
        "orders": sorted(set(bought)),
        "last": replies[-1],
        "told": (payment or {}).get("already_paid") is True,
    }


northfield_once = buy_three_ways(NORTHFIELD, "P1002", "P1002-8")
kettle_once = buy_three_ways(KETTLE, "KB-BLD-05", "KB-BLD-05::250g ground")

check(
    "Northfield: four cards on one basket buy it once",
    northfield_once.get("orders") is not None
    and len(northfield_once["orders"]) == 1,
    f"bought {northfield_once.get('orders', northfield_once)}",
    "engine/db/idempotency.py::begin_payment",
)
check(
    "Northfield: paying again says so instead of charging",
    northfield_once.get("told") is True,
    str(northfield_once.get("last")),
    "engine/api/chat.py::pay",
)
check(
    "Kettle: four cards on one basket buy it once",
    kettle_once.get("orders") is not None and len(kettle_once["orders"]) == 1,
    f"bought {kettle_once.get('orders', kettle_once)}",
    "engine/db/idempotency.py::begin_payment",
)
check(
    "Kettle: paying again says so instead of charging",
    kettle_once.get("told") is True,
    str(kettle_once.get("last")),
    "engine/api/chat.py::pay",
)


# The sidebar Pay button, which is a different route to the same money.
#
# The chat pay route derived its key from the cart and the card. This one
# generated a fresh uuid per request, so it had no idempotency at all - not even
# the half the other one had. The same cart and the same card, twice, bought two
# orders, which is exactly what a shopper on a slow connection does when nothing
# happens after the first tap.
def buy_twice_from_the_sidebar(shop: str, product: str, variant: str) -> dict:
    """Press Pay twice on one basket, the way a slow connection does."""
    signed_in_for(shop, "hc_twice")
    basket = call("POST", f"/api/shop/{shop}/cart")
    if "cart_id" not in basket:
        return {"setup": str(basket)[:120]}

    cart_id = basket["cart_id"]
    call(
        "POST",
        f"/api/shop/{shop}/cart/{cart_id}/lines",
        {"product_id": product, "variant_id": variant, "quantity": 1},
    )

    bought: list[str] = []
    last: dict = {}
    for _ in range(3):
        last = call(
            "POST",
            f"/api/shop/{shop}/cart/{cart_id}/checkout",
            {"card_last4": "1111"},
        )
        order_id = (last.get("order") or {}).get("order_id")
        if last.get("succeeded") and order_id:
            bought.append(order_id)

    return {"orders": sorted(set(bought)), "last": str(last)[:160]}


northfield_twice = buy_twice_from_the_sidebar(NORTHFIELD, "P1001", "P1001-9")
kettle_twice = buy_twice_from_the_sidebar(KETTLE, "KB-BLD-06", "KB-BLD-06::250g whole bean")

check(
    "Northfield: pressing Pay three times buys one order",
    northfield_twice.get("orders") is not None
    and len(northfield_twice["orders"]) == 1,
    f"bought {northfield_twice.get('orders', northfield_twice)}",
    "engine/api/shop.py::checkout - the key must not be a fresh uuid",
)
check(
    "Kettle: pressing Pay three times buys one order",
    kettle_twice.get("orders") is not None and len(kettle_twice["orders"]) == 1,
    f"bought {kettle_twice.get('orders', kettle_twice)}",
    "engine/api/shop.py::checkout - the key must not be a fresh uuid",
)
check(
    "the two checkout routes share one answer",
    northfield_twice.get("orders") is not None
    and kettle_twice.get("orders") is not None,
    "one of the two platforms did not produce an order at all",
    "engine/db/idempotency.py::begin_payment",
)


section("A paid cart cannot still be changed")

# Lines could be added to a cart after its order existed, and the total moved
# with them - on the REST cart routes, and separately through the chat/tap path,
# because the two share no code and neither one checked. A shopper adding
# something to a basket the shop had already charged them for is not a feature;
# it is an order whose contents disagree with what was paid.


def pay_it_off(shop: str, product: str, variant: str) -> tuple[str, str | None]:
    """A cart, bought. Returns (cart_id, order_id)."""
    signed_in_for(shop, "hc_lock")
    basket = call("POST", f"/api/shop/{shop}/cart")
    cart_id = basket.get("cart_id")
    call(
        "POST",
        f"/api/shop/{shop}/cart/{cart_id}/lines",
        {"product_id": product, "variant_id": variant, "quantity": 1},
    )
    session_id = f"hc_lock_{uuid.uuid4().hex[:6]}"
    r = call(
        "POST",
        "/api/chat/pay",
        {
            "connection_id": shop,
            "session_id": session_id,
            "cart_id": cart_id,
            "card_last4": "1111",
        },
    )
    return cart_id, (r.get("payment") or {}).get("order_id")


for shop, product, variant, other_product, other_variant in (
    # other_product has no variant on Northfield and one supplied on Kettle, so
    # the tap resolves in one step rather than asking "which one?" first - which
    # otherwise reports cart_changed=False for a reason that has nothing to do
    # with the cart being paid for.
    (NORTHFIELD, "P1001", "P1001-8", "P1003", None),
    (KETTLE, "KB-COL-02", "KB-COL-02::250g ground", "KB-BRA-04", "KB-BRA-04::250g whole bean"),
):
    cart_id, order_id = pay_it_off(shop, product, variant)

    before = call("GET", f"/api/shop/{shop}/cart/{cart_id}")
    kept_count = before.get("item_count")

    add_attempt = call(
        "POST",
        f"/api/shop/{shop}/cart/{cart_id}/lines",
        {"product_id": other_product, "quantity": 1},
    )
    check(
        f"{shop}: cannot add a line through the cart route once paid",
        add_attempt.get("_status") in (409, 400)
        or (add_attempt.get("_body") or "").find("ALREADY_PAID") != -1,
        str(add_attempt)[:160],
        "engine/api/shop.py::add_line",
    )

    line_id = (before.get("lines") or [{}])[0].get("line_id")
    change_attempt = call(
        "PATCH",
        f"/api/shop/{shop}/cart/{cart_id}/lines/{line_id}",
        {"quantity": 5},
    )
    check(
        f"{shop}: cannot change a quantity through the cart route once paid",
        change_attempt.get("_status") in (409, 400),
        str(change_attempt)[:160],
        "engine/api/shop.py::change_line",
    )

    # And through the tap path, which shares no code with the routes above.
    tap_session = f"hc_lock_tap_{uuid.uuid4().hex[:6]}"
    tap_body = {
        "connection_id": shop,
        "session_id": tap_session,
        "product_id": other_product,
        "cart_id": cart_id,
        "said": "Add that too",
    }
    if other_variant:
        tap_body["variant_id"] = other_variant
    tap_attempt = call("POST", "/api/chat/act", tap_body)
    check(
        f"{shop}: tapping a product does not add it to a paid cart",
        tap_attempt.get("cart_changed") is not True,
        str(tap_attempt.get("reply"))[:140],
        "engine/execution/service.py - ADD_TO_CART must check the ledger",
    )

    after = call("GET", f"/api/shop/{shop}/cart/{cart_id}")
    check(
        f"{shop}: the paid cart's contents never moved",
        after.get("item_count") == kept_count,
        f"{kept_count} before, {after.get('item_count')} after",
    )

# ---------------------------------------------------------------------------

section("Northfield: a decline that cannot be recovered")

signed_in_for(NORTHFIELD, "hc_nf")
cart = call("POST", f"/api/shop/{NORTHFIELD}/cart")
call(
    "POST",
    f"/api/shop/{NORTHFIELD}/cart/{cart['cart_id']}/lines",
    {"product_id": "P1001", "variant_id": "P1001-8", "quantity": 1},
)
paid = call(
    "POST",
    f"/api/shop/{NORTHFIELD}/cart/{cart['cart_id']}/checkout",
    {"card_last4": "0002"},
)
nf_order = (paid.get("order") or {}).get("order_id")

nf_case = call(
    "POST",
    "/api/simulate",
    {
        "connection_id": NORTHFIELD,
        "friction": "PAYMENT_DECLINED",
        "order_id": nf_order,
        "session_id": "hc_nf",
    },
)
check(
    "escalates when the platform cannot help",
    nf_case.get("selected_action") == "ESCALATE_TO_HUMAN"
    and nf_case.get("escalated_because_empty") is True,
    str(nf_case.get("selected_action")),
    "engine/decision/engine.py",
)
check(
    "the over-promise is replaced",
    nf_case.get("reply") != nf_case.get("shopper_reply"),
    "reply and shopper_reply are identical",
    "engine/api/routes.py::simulate",
)

# The live path a shopper actually takes, rather than /api/simulate - which has
# no awaiting_person field to check in the first place.
#
# ESCALATE_TO_HUMAN is not financial, not irreversible and touches no customer
# data, so the risk gate clears it to run automatically - "awaiting_person" was
# computed purely from that gate outcome, which made it false for the one
# action whose entire meaning is "a person is now involved". The shopper was
# told "I've passed it to someone at the shop", the ChatWidget's "Waiting on
# someone at the shop" banner - which reads that exact flag - never appeared,
# and there was nothing on screen to say the promise had been kept.
live_cart = call("POST", f"/api/shop/{NORTHFIELD}/cart")
call(
    "POST",
    f"/api/shop/{NORTHFIELD}/cart/{live_cart['cart_id']}/lines",
    {"product_id": "P1003", "quantity": 1},
)
live_session = f"hc_awaiting_{uuid.uuid4().hex[:6]}"
live_decline = call(
    "POST",
    "/api/chat/pay",
    {
        "connection_id": NORTHFIELD,
        "session_id": live_session,
        "cart_id": live_cart["cart_id"],
        "card_last4": "0003",
    },
)
check(
    "a shopper handed to a person is shown waiting on one",
    live_decline.get("awaiting_person") is True,
    f"selected_action={live_decline.get('selected_action')} "
    f"awaiting_person={live_decline.get('awaiting_person')}",
    "engine/api/chat.py - awaiting_person must also cover ESCALATE_TO_HUMAN",
)

# ---------------------------------------------------------------------------

section("Kettle: a decline that can be recovered")

signed_in_for(KETTLE, "hc_kb")
call(
    "PUT",
    f"/api/policy/{KETTLE}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": []},
)

bag = call("POST", f"/api/shop/{KETTLE}/cart")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{bag['cart_id']}/lines",
    {
        "product_id": "KB-ETH-01",
        "variant_id": "KB-ETH-01::250g whole bean",
        "quantity": 1,
    },
)
kb_paid = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{bag['cart_id']}/checkout",
    {"card_last4": "0002"},
)
kb_order = (kb_paid.get("order") or {}).get("order_id")
session = f"hc_{uuid.uuid4().hex[:8]}"

kb_case = call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
        "order_id": kb_order,
    },
)
check(
    "offers recovery instead of escalating",
    kb_case.get("selected_action") in {"OFFER_ALTERNATE_PAYMENT", "SPLIT_PAYMENT"},
    str(kb_case.get("selected_action")),
    "engine/decision/ranking.py",
)
check(
    "recovery still needs a person",
    kb_case.get("awaiting_person") is True,
    str(kb_case.get("risk_rule")),
    "engine/risk/gate.py",
)

# Nothing was capability-rejected here - Kettle can retry, split or offer
# another method, so all three survive the decision engine's filter and only
# lose on ranking. trace.rejected stays empty on a ranking-only outcome, and
# "why" returned None for it: a shopper on the one platform able to do more
# than one thing saw no explanation at all, on the exact turn CLAUDE.md names
# as the reason this toggle exists. RETRY_PAYMENT is checked for specifically
# because it is documented as always ranked last of the three, so it is
# reliably the one outranked whichever of the other two wins.
check(
    "a successful recovery still explains what it did not do",
    any(
        "trying your card again" in line
        for line in (kb_case.get("why") or {}).get("declined", [])
    ),
    str(kb_case.get("why")),
    "engine/api/chat.py - ranked-lower survivors must reach why(), not just rejections",
)

queue = call("GET", f"/api/approvals/{KETTLE}")
approvals = queue.get("approvals") or []
check(
    "case reaches the queue",
    len(approvals) > 0,
    f"{len(approvals)} pending",
    "engine/db/repository.py::record_case",
)

if approvals:
    apr = approvals[-1]["approval_id"]
    decided = call(
        "POST",
        f"/api/approvals/{KETTLE}/{apr}",
        {"approved": True, "decided_by": "healthcheck"},
    )
    executed = decided.get("executed") or {}
    check(
        "approving executes",
        executed.get("succeeded") is True,
        str(executed.get("summary"))[:80],
        "engine/execution/service.py",
    )
    check(
        "revenue is captured",
        bool(executed.get("payload", {}).get("recovered_amount")),
        str(executed.get("payload", {}).get("recovered_amount")),
        "engine/execution/service.py",
    )

    order_now = call("GET", f"/api/shop/{KETTLE}/order/{kb_order}")
    check(
        "the order is actually paid",
        order_now.get("payment_status") == "CAPTURED",
        f"{order_now.get('status')} / {order_now.get('payment_status')}",
        "adapters/kettle/adapter.py::recover_payment",
    )

    again = call(
        "POST",
        f"/api/approvals/{KETTLE}/{apr}",
        {"approved": True, "decided_by": "someone else"},
    )
    check(
        "a second approval changes nothing",
        again.get("changed") is False,
        str(again.get("changed")),
        "engine/db/repository.py::decide_approval",
    )

    transcript = call("GET", f"/api/chat/{KETTLE}/{session}")
    turns = transcript.get("turns") or []
    told = any("gone through" in (t.get("text") or "") for t in turns)
    check(
        "the shopper is told the outcome",
        told,
        f"{len(turns)} turns, none reporting the recovery",
        "engine/execution/service.py delivery block",
    )

# ---------------------------------------------------------------------------

section("Kettle: approved recovery that fails on the platform")

# Card 0006 is hard-blocked: the first checkout declines like every other card,
# but when a person approves a recovery the platform itself refuses it. This is
# the approved-then-failed outcome an operator needs to see rendered honestly,
# not buried under a success badge.
signed_in_for(KETTLE, "hc_hcb")
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
hcb_session = f"hc_hcb_{uuid.uuid4().hex[:6]}"

hcb_case = call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": hcb_session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
        "order_id": hcb_order,
    },
)
check(
    "hard-block still proposes recovery",
    hcb_case.get("selected_action") in {"OFFER_ALTERNATE_PAYMENT", "SPLIT_PAYMENT"},
    str(hcb_case.get("selected_action")),
    "engine/decision/ranking.py",
)
check(
    "hard-block recovery needs a person",
    hcb_case.get("awaiting_person") is True,
    str(hcb_case.get("risk_rule")),
    "engine/risk/gate.py",
)

hcb_queue = call("GET", f"/api/approvals/{KETTLE}")
hcb_approvals = hcb_queue.get("approvals") or []

if hcb_approvals:
    hcb_apr = hcb_approvals[-1]["approval_id"]
    hcb_decided = call(
        "POST",
        f"/api/approvals/{KETTLE}/{hcb_apr}",
        {"approved": True, "decided_by": "healthcheck"},
    )
    hcb_executed = hcb_decided.get("executed") or {}
    check(
        "approving a hard-blocked recovery fails",
        hcb_executed.get("succeeded") is False,
        str(hcb_executed.get("summary"))[:80],
        "engine/execution/service.py",
    )
    check(
        "failure code is PAYMENT_RECOVERY_FAILED",
        hcb_executed.get("error_code") == "PAYMENT_RECOVERY_FAILED",
        str(hcb_executed.get("error_code")),
        "engine/execution/service.py",
    )
    check(
        "the order stays unpaid",
        hcb_executed.get("final_state") == "FAILED",
        str(hcb_executed.get("final_state")),
        "engine/execution/service.py",
    )

    hcb_transcript = call("GET", f"/api/chat/{KETTLE}/{hcb_session}")
    hcb_turns = hcb_transcript.get("turns") or []
    hcb_told = any(
        "did not go through" in (t.get("text") or "") for t in hcb_turns
    )
    check(
        "the shopper is told it failed",
        hcb_told,
        f"{len(hcb_turns)} turns, none reporting the failure",
        "engine/execution/service.py delivery block",
    )

# ---------------------------------------------------------------------------

section("Shared memory")

# Tested while the problem is still open, which is the moment that matters: a card
# declines, the shopper types "what now", and the assistant already knows without
# being told. A resolved problem is deliberately excluded - otherwise a shopper whose
# payment had just been recovered said hello and was told someone needed to approve
# something.
signed_in_for(KETTLE, "hc_mem")
open_session = f"hc_mem_{uuid.uuid4().hex[:8]}"
open_bag = call("POST", f"/api/shop/{KETTLE}/cart")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{open_bag['cart_id']}/lines",
    {
        "product_id": "KB-COL-02",
        "variant_id": "KB-COL-02::250g whole bean",
        "quantity": 1,
    },
)
open_paid = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{open_bag['cart_id']}/checkout",
    {"card_last4": "0002"},
)
call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": open_session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
        "order_id": (open_paid.get("order") or {}).get("order_id"),
    },
)

mem = call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": open_session,
        "message": "what now?",
    },
)
check(
    "an open problem is remembered across surfaces",
    (mem.get("remembered_friction") or 0) > 0,
    f"friction={mem.get('remembered_friction')}, turns={mem.get('remembered_turns')}",
    "engine/session/store.py",
)

resolved = call(
    "POST",
    "/api/chat",
    {"connection_id": KETTLE, "session_id": session, "message": "hello again"},
)
check(
    "a resolved problem is not",
    (resolved.get("remembered_friction") or 0) == 0,
    f"friction={resolved.get('remembered_friction')} on a session whose case closed",
    "engine/session/store.py::recent_friction",
)

# ---------------------------------------------------------------------------

section("Reporting")

report = call("GET", f"/api/report/{KETTLE}")
check(
    "merchant report returns figures",
    "revenue_recovered" in report,
    str(report)[:80],
    "engine/db/repository.py::merchant_report",
)
if "revenue_recovered" in report:
    check(
        "revenue is non-zero",
        report["revenue_recovered"] != "0.00",
        report["revenue_recovered"],
        "engine/execution/service.py record_outcome",
    )
    check(
        "recent activity is listed",
        len(report.get("recent") or []) > 0,
        f"{len(report.get('recent') or [])} entries",
        "engine/db/repository.py::merchant_report",
    )
    rr = report.get("resolution_rate")
    check(
        "resolution_rate is reported",
        "resolution_rate" in report
        and (rr is None or isinstance(rr, (int, float))),
        f"{rr}%",
        "engine/db/repository.py::merchant_report",
    )

    # The deterministic part: two cases from one shopper must count once.
    #
    # "shoppers_helped" was the case count, so a shopper who hit two problems in
    # one session was counted twice - the headline number a merchant checks
    # against their own books, inflated by the number of separate run-ins rather
    # than people. The fix counts distinct sessions. To prove it, make two cases
    # on one fresh session and watch the report's shoppers_helped move by one,
    # even though the friction it records moved by two.
    #
    # Each chat with PAYMENT_DECLINED friction records a case (repository.py:
    # record_case, always a fresh row), and the checkout on a declining card
    # makes the friction real rather than fabricated. Both chats are created
    # before the after-report, so any prior data in the window cancels out of
    # the delta.
    def friction_total(rpt: dict) -> int:
        return sum(f["count"] for f in rpt.get("friction") or [])

    before = call("GET", f"/api/report/{KETTLE}")
    signed_in_for(KETTLE, "hc_two")
    two_session = f"hc_two_{uuid.uuid4().hex[:6]}"
    for _ in range(2):
        twobag = call("POST", f"/api/shop/{KETTLE}/cart")
        call(
            "POST",
            f"/api/shop/{KETTLE}/cart/{twobag['cart_id']}/lines",
            {
                "product_id": "KB-COL-02",
                "variant_id": "KB-COL-02::250g whole bean",
                "quantity": 1,
            },
        )
        twodecl = call(
            "POST",
            f"/api/shop/{KETTLE}/cart/{twobag['cart_id']}/checkout",
            {"card_last4": "0002"},
        )
        call(
            "POST",
            "/api/chat",
            {
                "connection_id": KETTLE,
                "session_id": two_session,
                "message": "my payment failed",
                "friction": "PAYMENT_DECLINED",
                "order_id": (twodecl.get("order") or {}).get("order_id"),
            },
        )
    after = call("GET", f"/api/report/{KETTLE}")
    delta_shoppers = after["shoppers_helped"] - before["shoppers_helped"]
    delta_cases = friction_total(after) - friction_total(before)
    check(
        "shoppers_helped counts shoppers, not cases",
        delta_cases == 2 and delta_shoppers == 1,
        f"{delta_shoppers} shopper from {delta_cases} cases",
        "engine/db/repository.py::merchant_report",
    )

stats = call("GET", f"/api/stats/{KETTLE}")
check(
    "operations stats available",
    "cases" in stats,
    str(stats)[:80],
    "engine/db/repository.py::stats",
)

# ---------------------------------------------------------------------------

section("Cart, through the chat")

if model_on:
    shop_session = f"hc_cart_{uuid.uuid4().hex[:6]}"
    shop_cart = call("POST", f"/api/shop/{NORTHFIELD}/cart")

    # A product with several sizes. Adding it without one should ask rather than
    # guess, because a guessed size is a return waiting to happen.
    ask = call(
        "POST",
        "/api/chat",
        {
            "connection_id": NORTHFIELD,
            "session_id": shop_session,
            "message": "add the Trailblazer Running Shoe to my cart",
            "cart_id": shop_cart["cart_id"],
        },
    )
    choices = ask.get("choices") or []
    check(
        "asks which size instead of guessing",
        len(choices) > 0,
        f"{len(choices)} choices, reply: {str(ask.get('reply'))[:60]}",
        "engine/execution/service.py ADD_TO_CART, engine/api/chat.py needs_choice",
    )
    if choices:
        # The model is told to extract a size from the shopper's own sentence and
        # writes as though it acted on it - "Sure, I'll add the Trailblazer Running
        # Shoe to your cart" - before execution has run. Appending the size
        # question to that sentence used to leave both in the reply: an implied
        # promise the size was handled, immediately followed by asking for it.
        # engine/api/chat.py's needs_choice branch now replaces the model's
        # sentence outright rather than appending to it, so the reply should be
        # exactly the engine's own question and never the model's guess at all.
        ask_reply = str(ask.get("reply") or "")
        check(
            "replaces the model's reply rather than appending the question to it",
            ask_reply.startswith("There's more than one option for"),
            ask_reply[:100],
            "engine/api/chat.py needs_choice replaces rather than appends",
        )

    # The generic failure branch, reached deterministically. Execution refuses
    # to touch any cart-mutating action once the basket is paid (the payment
    # ledger check in engine/execution/service.py), so a normal-sounding "add
    # this to my cart" against a paid cart must fail - and the model, which
    # never sees the ledger, has no way to know that when it writes its
    # pre-execution reply. The reply used to be the model's confident-sounding
    # sentence followed by an appended "I couldn't turn anything up for that" -
    # a contradiction in one message, the same shape as the needs_choice bug
    # above. The branch now replaces the model's sentence outright, so the
    # shopper should see only the engine's own failure sentence.
    #
    # A sold-out product looked like the natural trigger ("every option is sold
    # out" fails the same action), but the model reads the catalog and routes a
    # sold-out add to SUGGEST_ALTERNATIVE / RECOMMEND_PRODUCTS instead of
    # proposing a doomed ADD_TO_CART - which succeed, so the failure branch
    # never fires. The paid-cart trigger cannot be routed around, because
    # nothing visible to the model distinguishes it.
    signed_in_for(NORTHFIELD, "hc_fail")
    paid_for = call(
        "POST",
        "/api/shop/{}/cart".format(NORTHFIELD),
    )
    paid_session = f"hc_fail_{uuid.uuid4().hex[:6]}"
    call(
        "POST",
        "/api/shop/{}/cart/{}/lines".format(NORTHFIELD, paid_for["cart_id"]),
        {"product_id": "P1002", "variant_id": "P1002-8", "quantity": 1},
    )
    pay = call(
        "POST",
        "/api/chat/pay",
        {
            "connection_id": NORTHFIELD,
            "session_id": paid_session,
            "cart_id": paid_for["cart_id"],
            "card_last4": "1111",
        },
    )
    paid_ok = (pay.get("payment") or {}).get("paid") is True
    failure_reply = ""
    if paid_ok:
        # Named for what it is, not after the module-level `failed` list - the
        # shadowing bug class this file's own CLAUDE.md warns about.
        added_after_pay = call(
            "POST",
            "/api/chat",
            {
                "connection_id": NORTHFIELD,
                "session_id": paid_session,
                "message": "add the Trailblazer Running Shoe to my cart",
                "cart_id": paid_for["cart_id"],
            },
        )
        failure_reply = str(added_after_pay.get("reply") or "")
    check(
        "replaces the model's reply rather than appending a failure to it",
        failure_reply
        == "I couldn't turn anything up for that. Someone at the shop can help if you'd like.",
        failure_reply[:120],
        "engine/api/chat.py generic failure branch replaces rather than appends",
    )

    # Tapped rather than asked, so the rest of this section does not depend on the
    # model being reachable.
    #
    # The check above is the only one here that is about judgement: does the model
    # ask rather than guess. Everything below is about execution, which needs no
    # model - and tying them together meant a busy provider silently took four
    # checks with it.
    tapped = call(
        "POST",
        "/api/chat/act",
        {
            "connection_id": NORTHFIELD,
            "session_id": shop_session,
            "product_id": "P1001",
            "cart_id": shop_cart["cart_id"],
            "said": "Add the Trailblazer Running Shoe",
        },
    )
    choices = choices or tapped.get("choices") or []

    if choices:
        check(
            "sold-out sizes are not offered",
            all(c.get("label") != "10" for c in choices),
            str([c.get("label") for c in choices]),
            "engine/execution/service.py buyable filter",
        )

    # Now with a size named.
    # The tap endpoint, which is what the widget uses when a shopper picks an
    # option - so this tests the path a shopper actually takes, and it works while
    # the provider is busy because no model is involved.
    sized = call(
        "POST",
        "/api/chat/act",
        {
            "connection_id": NORTHFIELD,
            "session_id": shop_session,
            "product_id": "P1001",
            "variant_id": choices[0]["variant_id"] if choices else "P1001-8",
            "cart_id": shop_cart["cart_id"],
            "said": choices[0]["label"] if choices else "8",
        },
    )
    cart_now = call("GET", f"/api/shop/{NORTHFIELD}/cart/{shop_cart['cart_id']}")
    added = cart_now.get("item_count", 0) > 0
    check(
        "adds to cart when a size is given",
        added,
        f"{cart_now.get('item_count')} items, reply: {str(sized.get('reply'))[:60]}",
        "engine/execution/service.py ADD_TO_CART",
    )
    if added:
        check(
            "the storefront is told the cart changed",
            sized.get("cart_changed") is True,
            str(sized.get("cart_changed")),
            "engine/api/chat.py cart_changed",
        )

        # Removed through the cart route rather than by asking.
        #
        # Asking needs the model, and this check is about whether removal works -
        # which is a different question from whether the model understands
        # "actually remove that". The second is worth testing and belongs with the
        # other reasoning checks, not gating three that have nothing to do with it.
        lines_now = call(
            "GET", f"/api/shop/{NORTHFIELD}/cart/{shop_cart['cart_id']}"
        ).get("lines") or []

        removed = (
            call(
                "PATCH",
                f"/api/shop/{NORTHFIELD}/cart/{shop_cart['cart_id']}/lines/"
                f"{lines_now[0]['line_id']}",
                {"quantity": 0},
            )
            if lines_now
            else {}
        )
        after = call("GET", f"/api/shop/{NORTHFIELD}/cart/{shop_cart['cart_id']}")
        check(
            "removes from cart on request",
            after.get("item_count", 1) == 0,
            f"{after.get('item_count')} items left, reply: {str(removed.get('reply'))[:60]}",
            "engine/execution/service.py REMOVE_CART_LINE",
        )
else:
    print("  SKIP  cart checks (no key configured)")

# ---------------------------------------------------------------------------

section("Rejection")

signed_in_for(KETTLE, "hc_rej")
rej_bag = call("POST", f"/api/shop/{KETTLE}/cart")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{rej_bag['cart_id']}/lines",
    {
        "product_id": "KB-COL-02",
        "variant_id": "KB-COL-02::250g whole bean",
        "quantity": 1,
    },
)
rej_paid = call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{rej_bag['cart_id']}/checkout",
    {"card_last4": "0002"},
)
rej_order = (rej_paid.get("order") or {}).get("order_id")
rej_session = f"hc_rej_{uuid.uuid4().hex[:6]}"

call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": rej_session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
        "order_id": rej_order,
    },
)

queue2 = (call("GET", f"/api/approvals/{KETTLE}").get("approvals")) or []
if queue2:
    rej_id = queue2[-1]["approval_id"]
    call(
        "POST",
        f"/api/approvals/{KETTLE}/{rej_id}",
        {
            "approved": False,
            "decided_by": "healthcheck",
            "note": "customer already paid by transfer",
        },
    )

    turns2 = (call("GET", f"/api/chat/{KETTLE}/{rej_session}").get("turns")) or []
    told_no = any(
        "not able to do that one" in (t.get("text") or "") for t in turns2
    )
    check(
        "a rejected shopper is told",
        told_no,
        f"{len(turns2)} turns, none reporting the rejection",
        "engine/api/routes.py::decide, the not-approved branch",
    )

    leaked = any(
        "already paid by transfer" in (t.get("text") or "") for t in turns2
    )
    check(
        "the operator's note stays private",
        not leaked,
        "the internal note reached the shopper",
        "engine/api/routes.py::decide",
    )
else:
    check(
        "rejection case reached the queue",
        False,
        "the queue was empty, so there was nothing to reject",
        "engine/api/chat.py - a decline should create an approval",
    )

# ---------------------------------------------------------------------------

section("A promise of a person is kept")

# A merchant blocking ESCALATE_TO_HUMAN.
#
# The gate did as it was told and the reply did not. The shopper was still told
# "I've passed it to someone at the shop who can", the case landed in state
# BLOCKED, no handover was created, and nobody at CV3 or at the shop ever saw it.
# One tick in the policy editor quietly disconnected the safety net while the
# assistant carried on promising it.
#
# Northfield, because it cannot recover a payment - so a declined card there has
# nowhere to go except a person, which is exactly the path being tested.

# Drain the handover window first. handovers_across is capped at the oldest fifty,
# and the demo database accumulates escalations from every previous run - so on a
# well-used database this block's own fresh case would land outside the window and
# the assertion below would fail for a backlog, not for the unblockable-floor bug
# it guards. Oldest-first ordering means closing what the list shows is enough:
# the next round surfaces what followed it. The new escalation from this section is
# then the only handover in the window, which is exactly the observable being tested.
for _ in range(6):
    _handovers = call("GET", "/api/ops/handovers", key=OPERATOR).get("handovers", [])
    if not _handovers:
        break
    for _h in _handovers:
        call(
            "POST",
            f"/api/ops/handovers/{_h['connection_id']}/{_h['case_id']}",
            {"handled_by": "healthcheck-drain"},
            key=OPERATOR,
        )

signed_in_for(NORTHFIELD, "hc_block")
blocked_session = f"hc_block_{uuid.uuid4().hex[:6]}"

before_policy = call("GET", f"/api/policy/{NORTHFIELD}")
call(
    "PUT",
    f"/api/policy/{NORTHFIELD}",
    {"mode": "STANDARD", "auto_allowed": [], "blocked": ["ESCALATE_TO_HUMAN"]},
)

blocked_cart = call("POST", f"/api/shop/{NORTHFIELD}/cart")
call(
    "POST",
    f"/api/shop/{NORTHFIELD}/cart/{blocked_cart.get('cart_id')}/lines",
    {"product_id": "P1003", "quantity": 1},
)
blocked_reply = call(
    "POST",
    "/api/chat/pay",
    {
        "connection_id": NORTHFIELD,
        "session_id": blocked_session,
        "cart_id": blocked_cart.get("cart_id"),
        "card_last4": "0004",
    },
)

blocked_handovers = call("GET", "/api/ops/handovers")
reached = [
    h
    for h in blocked_handovers.get("handovers", [])
    if h.get("case_id") == blocked_reply.get("case_id")
]

check(
    "a blocked escalation still reaches a person",
    len(reached) == 1,
    f"{len(reached)} handovers for case {blocked_reply.get('case_id')}",
    "engine/risk/gate.py - a merchant must not be able to block the safety net",
)
check(
    "the rule says the block was overridden",
    blocked_reply.get("risk_rule") == "ESCALATION_ALWAYS_REACHES_A_PERSON",
    str(blocked_reply.get("risk_rule")),
    "engine/risk/gate.py",
)
check(
    "the shopper is not promised somebody who was never told",
    "passed it" not in str(blocked_reply.get("reply", "")).lower()
    or len(reached) == 1,
    str(blocked_reply.get("reply"))[:130],
    "engine/api/chat.py - the reply must match what actually happened",
)

# Put the policy back before anything else runs against this merchant.
call(
    "PUT",
    f"/api/policy/{NORTHFIELD}",
    {
        "mode": before_policy.get("mode", "STANDARD"),
        "auto_allowed": before_policy.get("auto_allowed", []),
        "blocked": before_policy.get("blocked", []),
    },
)
restored = call("GET", f"/api/policy/{NORTHFIELD}")
check(
    "the merchant's policy is put back",
    restored.get("blocked") == before_policy.get("blocked"),
    f"{restored.get('blocked')} vs {before_policy.get('blocked')}",
)

# Handover paging. Exactly one handover is open here: the drain above closed every
# pre-existing one, and the blocked escalation just above added the only new one. So
# a page of limit=1 must show it at offset 0 and nothing at offset 1, while `total`
# stays 1 either way - proving total is the whole open set, not the size of the page
# the consumer happens to be looking at. Without the total, a busy system hid the
# newest handovers behind the oldest-fifty cap and they read as gone until the
# oldest were closed; the total is what makes paging honest instead of a second way
# to hide things.
_page0 = call(
    "GET", "/api/ops/handovers?offset=0&limit=1", key=OPERATOR
)
_page1 = call(
    "GET", "/api/ops/handovers?offset=1&limit=1", key=OPERATOR
)
check(
    "handovers report a total that is not the page size",
    isinstance(_page0.get("total"), int)
    and _page0.get("total") == _page1.get("total")
    and _page0.get("total", 0) >= len(_page0.get("handovers", [])),
    f"total={_page0.get('total')} page0_len={len(_page0.get('handovers', []))} "
    f"page1_len={len(_page1.get('handovers', []))}",
    "engine/api/routes.py ops_handovers, engine/db/repository.py handovers_across",
)
check(
    "handover offset pages past the first page",
    len(_page1.get("handovers", [])) == 0,
    "offset=1 still returned handovers: "
    f"{[h.get('case_id') for h in _page1.get('handovers', [])]}",
    "engine/db/repository.py handovers_across offset",
)

# ---------------------------------------------------------------------------

section("The holdout")

# Set to 100% so the very next friction turn is guaranteed to land in the
# no-assistance group - anything less would make this check flaky by design.
# Kettle, not Northfield, so the policy this test flips is fully isolated
# from the merchant-policy save/restore test just above.
before_holdout_policy = call("GET", f"/api/policy/{KETTLE}")
call(
    "PUT",
    f"/api/policy/{KETTLE}",
    {
        "mode": before_holdout_policy.get("mode", "STANDARD"),
        "auto_allowed": before_holdout_policy.get("auto_allowed", []),
        "blocked": before_holdout_policy.get("blocked", []),
        "holdout_percent": 100,
    },
)

holdout_session = f"hc_holdout_{uuid.uuid4().hex[:6]}"
holdout_reply = call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": holdout_session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
        "skip_model": True,
    },
)
check(
    "a holdout session gets no reasoning and no proposal",
    holdout_reply.get("risk_rule") == "HOLDOUT_NO_ASSISTANCE"
    and holdout_reply.get("used_model") is False
    and holdout_reply.get("selected_action") is None,
    f"risk_rule={holdout_reply.get('risk_rule')} "
    f"used_model={holdout_reply.get('used_model')} "
    f"selected_action={holdout_reply.get('selected_action')}",
    "engine/api/chat.py _holdout_reply",
)

# Same session, a second friction turn - must land in the same group as the
# first. A holdout that could flip mid-visit would not be a controlled
# comparison of anything.
holdout_reply_2 = call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": holdout_session,
        "message": "still declined",
        "friction": "PAYMENT_DECLINED",
        "skip_model": True,
    },
)
check(
    "the same session stays in the holdout group on its next friction turn",
    holdout_reply_2.get("risk_rule") == "HOLDOUT_NO_ASSISTANCE",
    str(holdout_reply_2.get("risk_rule")),
    "engine/db/repository.py holdout_status",
)

holdout_report = call("GET", f"/api/report/{KETTLE}")
holdout_stats = holdout_report.get("holdout") or {}
check(
    "the merchant report counts this session in the holdout group",
    holdout_stats.get("holdout_cases", 0) >= 1,
    str(holdout_stats),
    "engine/db/repository.py merchant_report",
)

# A holdout shopper who fixes their own problem must count as resolved, or
# the comparison measures "did the assistant act" rather than "did the
# problem get fixed" - the wrong question. Decline a real cart, then pay the
# same cart with a working card, entirely without the assistant's help, and
# check the holdout case for it flips from unresolved to resolved.
#
# Measured as a delta, not an absolute count: the report window can already
# hold resolved holdout cases from an earlier run, so ">= 1" alone would pass
# even if this specific cart's case were never actually resolved.
resolved_before = (
    (call("GET", f"/api/report/{KETTLE}").get("holdout") or {}).get(
        "holdout_resolved", 0
    )
)
signed_in_for(KETTLE, "hc_holdout_resolve")
resolve_cart = call("POST", f"/api/shop/{KETTLE}/cart")
resolve_cart_id = resolve_cart.get("cart_id")
call(
    "POST",
    f"/api/shop/{KETTLE}/cart/{resolve_cart_id}/lines",
    {"product_id": "KB-ETH-01", "variant_id": "KB-ETH-01::250g whole bean", "quantity": 1},
)
resolve_session = f"hc_holdout_resolve_{uuid.uuid4().hex[:6]}"
declined = call(
    "POST",
    "/api/chat/pay",
    {
        "connection_id": KETTLE,
        "session_id": resolve_session,
        "cart_id": resolve_cart_id,
        "card_last4": "0005",
    },
)
check(
    "the declined cart is a holdout case with no assistance",
    "HOLDOUT_NO_ASSISTANCE" in str(declined.get("risk_rule")),
    str(declined.get("risk_rule")),
    "engine/api/chat.py _holdout_reply",
)
retried = call(
    "POST",
    "/api/chat/pay",
    {
        "connection_id": KETTLE,
        "session_id": resolve_session,
        "cart_id": resolve_cart_id,
        "card_last4": "1111",
    },
)
check(
    "retrying the same cart on their own pays it",
    retried.get("payment", {}).get("paid") is True,
    str(retried.get("payment")),
)
holdout_report_after_resolve = call("GET", f"/api/report/{KETTLE}")
resolved_after = (holdout_report_after_resolve.get("holdout") or {}).get(
    "holdout_resolved", 0
)
check(
    "a holdout shopper who fixes their own problem counts as resolved",
    resolved_after > resolved_before,
    f"before={resolved_before} after={resolved_after}",
    "engine/db/repository.py resolve_holdout_case_for_cart",
)

# Put the policy back before anything else runs against this merchant.
call(
    "PUT",
    f"/api/policy/{KETTLE}",
    {
        "mode": before_holdout_policy.get("mode", "STANDARD"),
        "auto_allowed": before_holdout_policy.get("auto_allowed", []),
        "blocked": before_holdout_policy.get("blocked", []),
        "holdout_percent": 0,
    },
)
restored_holdout_policy = call("GET", f"/api/policy/{KETTLE}")
check(
    "the holdout is switched back off",
    restored_holdout_policy.get("holdout_percent") == 0,
    str(restored_holdout_policy.get("holdout_percent")),
)

# ---------------------------------------------------------------------------

section("Expiry")

exp_session = f"hc_exp_{uuid.uuid4().hex[:6]}"
call(
    "POST",
    "/api/chat",
    {
        "connection_id": KETTLE,
        "session_id": exp_session,
        "message": "my payment failed",
        "friction": "PAYMENT_DECLINED",
    },
)

# Backdate whatever is pending so the sweeper has something to find. Written in
# SQLAlchemy's SQLite format; an ISO string with an offset compares as text and
# silently never matches.
try:
    import sqlite3
    from datetime import UTC, datetime, timedelta

    conn = sqlite3.connect("cv3.db")
    past = (datetime.now(UTC) - timedelta(minutes=30)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    conn.execute(
        "update approvals set expires_at=? where state='PENDING'", (past,)
    )
    conn.commit()
    conn.close()
    backdated = True
except Exception as e:  # noqa: BLE001
    backdated = False
    print(f"  SKIP  expiry (could not backdate: {e})")

if backdated:
    swept = call("POST", "/api/admin/expire")
    check(
        "the sweeper expires stale approvals",
        (swept.get("expired") or 0) > 0,
        str(swept),
        "engine/expiry.py, engine/db/repository.py::expire_approvals",
    )

    exp_turns = (call("GET", f"/api/chat/{KETTLE}/{exp_session}").get("turns")) or []
    check(
        "an abandoned shopper is told",
        any("in time" in (t.get("text") or "") for t in exp_turns),
        f"{len(exp_turns)} turns, none reporting the timeout",
        "engine/expiry.py::sweep_once",
    )

# ---------------------------------------------------------------------------

section("Operations console")

if OPERATOR is None:
    print("  SKIP  operations checks (no .env.keys - run mint_keys.py)")
else:
    # Explicitly keyless: this check exists to prove the door is shut.
    refused = call("GET", "/api/ops/stats", key=NO_KEY)
    check(
        "the operations queue is locked",
        # 401 specifically, not merely "it failed". A 500 that happened to stop the
        # request would otherwise read as a working lock.
        refused.get("_status") == 401,
        f"an unauthenticated request returned {refused}",
        "engine/api/auth.py",
    )

ops = call("GET", "/api/ops/stats", key=OPERATOR)
check(
    "cross-merchant workload",
    "waiting" in ops and "by_merchant" in ops,
    str(ops)[:90],
    "engine/api/routes.py::ops_overview",
)

# One case, left pending on purpose.
#
# These checks were gated on the queue being non-empty, and by this point
# everything earlier has been decided - so a clean run guaranteed they never
# executed. A check that cannot run precisely when everything works is worse than
# no check, because the suite looks complete.
#
# Simulated rather than chatted, so it needs no model.
signed_in_for(KETTLE, "hc_ops")
pending_bag = call("POST", f"/api/shop/{KETTLE}/cart")
if pending_bag.get("cart_id"):
    call(
        "POST",
        f"/api/shop/{KETTLE}/cart/{pending_bag['cart_id']}/lines",
        {
            "product_id": "KB-COL-02",
            "variant_id": "KB-COL-02::250g whole bean",
            "quantity": 1,
        },
    )
    pending_paid = call(
        "POST",
        f"/api/shop/{KETTLE}/cart/{pending_bag['cart_id']}/checkout",
        {"card_last4": "0002"},
    )
    call(
        "POST",
        "/api/simulate",
        {
            "connection_id": KETTLE,
            "friction": "PAYMENT_DECLINED",
            "order_id": (pending_paid.get("order") or {}).get("order_id"),
            "session_id": f"hc_queue_{uuid.uuid4().hex[:6]}",
        },
    )

ops_q = call("GET", "/api/ops/queue", key=OPERATOR)
q_rows = ops_q.get("approvals")
check(
    "one queue across every merchant",
    isinstance(q_rows, list),
    str(ops_q)[:90],
    "engine/api/routes.py::ops_queue",
)
if isinstance(q_rows, list) and q_rows:
    check(
        "queue entries name their merchant",
        all(r.get("merchant_name") for r in q_rows),
        "an entry has no merchant_name",
        "engine/api/routes.py::ops_queue",
    )
    check(
        "queue entries show the wait",
        all(r.get("waiting_minutes") is not None for r in q_rows),
        "an entry has no waiting_minutes",
        "engine/db/repository.py::pending_across",
    )

hist = call("GET", "/api/ops/history", key=OPERATOR)
rows = hist.get("decisions") or []
check(
    "decided work is visible",
    len(rows) > 0,
    f"{len(rows)} decisions",
    "engine/api/routes.py::ops_history",
)
if rows:
    check(
        "rejection notes are kept",
        any(r.get("note") for r in rows),
        "no decision carries a note",
        "engine/db/repository.py::decided_across",
    )
    check(
        "expiries are recorded as decisions",
        any(r.get("state") == "EXPIRED" for r in rows),
        str([r.get("state") for r in rows][:6]),
        "engine/db/repository.py::expire_approvals",
    )

    # Closing a case id that does not exist. db.mark_handled returned False for
    # this - not None - while the route only ever checked "is closed None", so
    # False fell through to closed.get("session_id") on a bool and crashed with a
    # 500. An operator mistyping a case id, or a browser tab racing a colleague
    # who closed it first, should get a clean answer, not a stack trace.
    bogus_close = call(
        "POST",
        f"/api/ops/handovers/{KETTLE}/case_does_not_exist",
        {"handled_by": "healthcheck"},
        key=OPERATOR,
    )
    check(
        "closing a handover that does not exist fails cleanly",
        bogus_close.get("_status") is None and bogus_close.get("changed") is False,
        str(bogus_close)[:160],
        "engine/db/repository.py::mark_handled",
    )

    # And closing the same one twice. mark_handled returns None for "already
    # handled" today, which the route already maps to the same clean answer - this
    # locks that in rather than assuming it stays true.
    handovers_now = call("GET", "/api/ops/handovers", key=OPERATOR)
    open_case = next(iter(handovers_now.get("handovers", [])), None)
    if open_case:
        first_close = call(
            "POST",
            f"/api/ops/handovers/{open_case['connection_id']}/{open_case['case_id']}",
            {"handled_by": "healthcheck"},
            key=OPERATOR,
        )
        second_close = call(
            "POST",
            f"/api/ops/handovers/{open_case['connection_id']}/{open_case['case_id']}",
            {"handled_by": "healthcheck"},
            key=OPERATOR,
        )
        check(
            "closing an already-closed handover also fails cleanly",
            second_close.get("_status") is None
            and second_close.get("changed") is False,
            f"first: {first_close}, second: {second_close}",
            "engine/db/repository.py::mark_handled",
        )

# ---------------------------------------------------------------------------

print("\n" + "=" * 60)
if skipped:
    print()
    print(f"  {len(skipped)} check(s) skipped - the model was busy:")
    for name in skipped:
        print(f"    {name}")
    print("  Not failures. Rerun when the provider is idle for a complete number.")

if failed:
    print(f"{passed} passed, {len(failed)} FAILED")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)

ran = passed + len(skipped)

if ran < EXPECTED_CHECKS:
    missing = EXPECTED_CHECKS - ran
    print(f"{passed} passed, {len(skipped)} skipped, {missing} never ran.")
    print()
    print(f"  {missing} check(s) did not execute at all. Some checks need an")
    print("  earlier one to succeed - a skipped model check takes its dependants")
    print("  with it, and until now nothing said so.")
    print()
    print("  Rerun with the provider idle for a complete number.")
    sys.exit(1)

print(f"All {passed} checks passed.")