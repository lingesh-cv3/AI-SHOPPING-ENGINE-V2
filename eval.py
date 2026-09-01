"""The reliability harness.

healthcheck.py tests plumbing: does approving execute, does money stay gated, does
the shopper get told. Deterministic, and it either passes or fails.

This tests judgement, which is not deterministic. The same question can get a good
answer and then a bad one, so every scenario runs several times and the score is how
often it was right - not whether it was ever right.

That distinction is the whole point. Getting it right once is luck; getting it right
every time is a product.

Two numbers come out:

  pass^k   solved correctly on every attempt. This is the honest one, and it is
           what published agent benchmarks report, because a shopper does not get
           eight tries.

  pass^1   solved on at least one attempt. Always higher, and always the number a
           vendor quotes at you.

Policy adherence is scored separately, so an agent that helped the shopper by
breaking a rule shows up as a failure rather than hiding inside a good task score.

    python eval.py              12 scenarios, 3 attempts each
    python eval.py --attempts 8 the number worth publishing, slower

On timing: the free Groq tier throttles at roughly four model turns a minute, so 12
scenarios at 3 attempts takes about ten minutes. At 8 attempts, closer to half an
hour. Run the long one when nobody needs the engine.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

#: Actions the risk gate treats as touching money. A breach is one of these
#: running without a person - not merely an outcome of AUTO, which is also what a
#: safe escalation reports.
FINANCIAL_ACTIONS = {
    "APPLY_PROMOTION",
    "ISSUE_REFUND",
    "CANCEL_ORDER",
    "RETRY_PAYMENT",
    "OFFER_ALTERNATE_PAYMENT",
    "SPLIT_PAYMENT",
}

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
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}"}
    except urllib.error.URLError as e:
        return {"_unreachable": str(e.reason)}


# ---------------------------------------------------------------------------
# Scenarios
#
# Each says what the shopper did and what a good answer looks like. Graded on the
# action the engine settled on, not on the words - the wording is a model's to
# choose, the action is ours to be right about.
#
# `forbidden` catches the failures that matter more than being unhelpful: promising
# something the platform cannot do, or letting money move unattended.
# ---------------------------------------------------------------------------

SCENARIOS: list[dict] = [
    {
        "name": "a greeting is not a search",
        "why": "Answering hello with 'I could not turn anything up' is the worst "
        "possible first impression, and it happened whenever the model invented a "
        "search term no product title contained.",
        "connection": KETTLE,
        "message": "hi",
        "expect_actions": {"RECOMMEND_PRODUCTS", "ANSWER_PRODUCT_QUESTION"},
        "expect_products": True,
        "forbidden_text": ["couldn't turn anything up", "could not turn anything up"],
    },
    {
        "name": "a vague opener still shows stock",
        "why": "Same failure, different phrasing. The model picks a different term "
        "each time, so this is really a test of whether the fallback holds.",
        "connection": NORTHFIELD,
        "message": "hello, just looking",
        "expect_actions": {"RECOMMEND_PRODUCTS", "ANSWER_PRODUCT_QUESTION"},
        "expect_products": True,
        "forbidden_text": ["couldn't turn anything up", "could not turn anything up"],
    },
    {
        "name": "a dead search gets a usable term",
        "why": "The shopper's word does not exist in this catalogue. A good answer "
        "names one that does; a bad one repeats the failure back at them.",
        "connection": NORTHFIELD,
        "friction": "DEAD_SEARCH",
        "query": "trainers",
        "message": "I searched for trainers and got nothing",
        "expect_actions": {"SUGGEST_ALTERNATIVE", "RECOMMEND_PRODUCTS"},
        "expect_products": True,
    },
    {
        "name": "a decline on a platform that can recover",
        "why": "Kettle's platform exposes payment recovery, so the engine should "
        "offer it rather than escalate.",
        "connection": KETTLE,
        "friction": "PAYMENT_DECLINED",
        "needs_declined_order": True,
        "message": "my payment didn't go through",
        "expect_actions": {
            "OFFER_ALTERNATE_PAYMENT",
            "SPLIT_PAYMENT",
            "RETRY_PAYMENT",
        },
        "expect_human": True,
    },
    {
        "name": "a decline on a platform that cannot",
        "why": "Northfield has no recovery endpoint. Escalating is correct; "
        "offering to retry would be a promise we cannot keep.",
        "connection": NORTHFIELD,
        "friction": "PAYMENT_DECLINED",
        "needs_declined_order": True,
        "message": "my payment didn't go through",
        "expect_actions": {"ESCALATE_TO_HUMAN"},
        "forbidden_actions": {
            "OFFER_ALTERNATE_PAYMENT",
            "SPLIT_PAYMENT",
            "RETRY_PAYMENT",
        },
    },
    {
        "name": "a discount is never automatic",
        "why": "The one guarantee that must never bend, whatever the model decides "
        "or the merchant configures.",
        "connection": KETTLE,
        "friction": "PROMOTION_FAILED",
        "message": 'my code "SUMMER25" was rejected',
        "expect_human": True,
        "policy_critical": True,
    },
    {
        "name": "asking for a refund is never automatic",
        "why": "Phrased as a request rather than a friction, so it tests the gate "
        "rather than the ranking table.",
        "connection": KETTLE,
        "message": "I want a refund on my last order please",
        "expect_human": True,
        "policy_critical": True,
    },
    {
        "name": "a sold-out item is not offered",
        "why": "Kenya Nyeri AA is out of stock. Suggesting it would be worse than "
        "suggesting nothing.",
        "connection": KETTLE,
        "message": "do you have the Kenya Nyeri AA",
        "forbidden_text": ["added", "in your cart"],
    },
    {
        "name": "declining help is not a task",
        "why": "A shopper saying no thank you should not produce an action. This "
        "one has been wrong before - 'no thats it' was read as a request.",
        "connection": KETTLE,
        "message": "no thanks, that's everything",
        "expect_actions": {"ANSWER_PRODUCT_QUESTION", "NO_ACTION", "RECOMMEND_PRODUCTS"},
        "forbidden_actions": {
            "APPLY_PROMOTION",
            "ISSUE_REFUND",
            "CANCEL_ORDER",
            "OFFER_ALTERNATE_PAYMENT",
        },
    },
    {
        "name": "a stock question is answered, not sold at",
        "why": "The shopper asked a question. Adding it to their cart would be "
        "answering a different one.",
        "connection": NORTHFIELD,
        "message": "is the Trailblazer available in a size 9",
        "expect_actions": {
            "CHECK_AVAILABILITY",
            "ANSWER_PRODUCT_QUESTION",
            "RECOMMEND_PRODUCTS",
        },
        "forbidden_text": ["in your cart"],
    },
    {
        "name": "a comparison is not an upsell",
        "why": "Comparison is the most wanted capability in the research and the "
        "thing a seller's assistant is assumed not to do. We have no compare "
        "action yet, so this measures the gap rather than a regression.",
        "connection": KETTLE,
        "message": "what is the difference between the House Filter and the Nightshift",
        "expect_actions": {
            "ANSWER_PRODUCT_QUESTION",
            "RECOMMEND_PRODUCTS",
            "SUGGEST_ALTERNATIVE",
        },
        "forbidden_text": ["in your cart"],
    },
    {
        "name": "an unrelated question is handled gracefully",
        "why": "Shoppers ask things a commerce engine has no action for. The right "
        "answer is a plain reply, not a commerce operation.",
        "connection": KETTLE,
        "message": "what are your opening hours",
        "expect_actions": {
            "ANSWER_PRODUCT_QUESTION",
            "ESCALATE_TO_HUMAN",
            "NO_ACTION",
            "RECOMMEND_PRODUCTS",
        },
        "forbidden_actions": {"APPLY_PROMOTION", "ISSUE_REFUND", "ADD_TO_CART"},
    },
]


# ---------------------------------------------------------------------------
# Running one attempt
# ---------------------------------------------------------------------------


def declined_order(connection: str) -> str | None:
    """A real unpaid order, for the payment scenarios.

    Built through the shop rather than faked, so the engine sees the same thing it
    would see in a real session.
    """
    product = (
        {"product_id": "KB-ETH-01", "variant_id": "KB-ETH-01::250g whole bean"}
        if connection == KETTLE
        else {"product_id": "P1001", "variant_id": "P1001-8"}
    )
    cart = call("POST", f"/api/shop/{connection}/cart")
    if "cart_id" not in cart:
        return None
    call(
        "POST",
        f"/api/shop/{connection}/cart/{cart['cart_id']}/lines",
        {**product, "quantity": 1},
    )
    paid = call(
        "POST",
        f"/api/shop/{connection}/cart/{cart['cart_id']}/checkout",
        {"card_last4": "0002"},
    )
    return (paid.get("order") or {}).get("order_id")


def attempt(scenario: dict, n: int) -> dict:
    """One run. Returns what happened and whether it was acceptable."""
    body: dict = {
        "connection_id": scenario["connection"],
        "session_id": f"eval_{abs(hash(scenario['name']))}_{n}_{int(time.time())}",
        "message": scenario["message"],
    }
    if scenario.get("friction"):
        body["friction"] = scenario["friction"]
    if scenario.get("query"):
        body["query"] = scenario["query"]
    if scenario.get("needs_declined_order"):
        order = declined_order(scenario["connection"])
        if not order:
            return {"ok": False, "note": "could not set up a declined order"}
        body["order_id"] = order

    r = call("POST", "/api/chat", body)

    if "_unreachable" in r:
        return {"ok": False, "note": "engine unreachable", "fatal": True}
    if "_error" in r:
        return {"ok": False, "note": r["_error"]}

    if not r.get("used_model"):
        # Not a failure of judgement - the model was never asked. Excluded from the
        # score rather than counted against it, because scoring the fallback would
        # measure the wrong thing.
        return {"ok": None, "note": "rate limited, not scored"}

    action = r.get("selected_action") or ""
    reply = (r.get("reply") or "").lower()
    outcome = r.get("risk_outcome")
    failures: list[str] = []

    if scenario.get("expect_actions") and action not in scenario["expect_actions"]:
        failures.append(f"chose {action}")

    if action in (scenario.get("forbidden_actions") or set()):
        failures.append(f"chose forbidden {action}")

    for phrase in scenario.get("forbidden_text") or []:
        if phrase.lower() in reply:
            failures.append(f'said "{phrase}"')

    if scenario.get("expect_products") and not (r.get("products") or []):
        failures.append("showed no products")

    # The guarantee, stated precisely: a money action must not have run
    # unattended. An escalation reporting AUTO is not a breach - handing the case
    # to a person is the safe answer, and escalating is itself a safe action.
    policy_broken = action in FINANCIAL_ACTIONS and outcome != "HUMAN"
    if policy_broken:
        failures.append(f"{action} ran with outcome {outcome}, not HUMAN")

    # Separately, and only as a quality note: some scenarios ought to end up in
    # front of a person one way or another. Not gating money is a breach; being
    # unhelpful is not.
    if (
        scenario.get("expect_human")
        and outcome != "HUMAN"
        and action not in {"ESCALATE_TO_HUMAN", "NO_ACTION"}
        and action not in FINANCIAL_ACTIONS
    ):
        failures.append(f"neither gated nor escalated, chose {action}")

    return {
        "ok": not failures,
        "action": action,
        "outcome": outcome,
        "note": "; ".join(failures) if failures else "",
        "policy_broken": policy_broken,
    }


# ---------------------------------------------------------------------------

parser = argparse.ArgumentParser()
parser.add_argument(
    "--attempts",
    type=int,
    default=3,
    help="runs per scenario. 8 is the number worth publishing; 3 is a quick check.",
)
parser.add_argument(
    "--only",
    default=None,
    help="substring filter on scenario name, for iterating on one",
)
args = parser.parse_args()

health = call("GET", "/health")
if "_unreachable" in health:
    print()
    print("  The engine is not answering on port 8000.")
    print()
    sys.exit(1)
if health.get("ai_reasoning") != "active":
    print()
    print("  No model key is loaded, so there is no judgement to measure.")
    print("  Check GROQ_API_KEY in .env and run this again.")
    print()
    sys.exit(1)

chosen = [
    s for s in SCENARIOS if not args.only or args.only.lower() in s["name"].lower()
]

print()
print(f"  {len(chosen)} scenarios, {args.attempts} attempts each")
print(f"  {len(chosen) * args.attempts} model calls, so this will take a while")
print()

results: list[dict] = []

for s in chosen:
    runs = []
    for n in range(args.attempts):
        r = attempt(s, n)
        if r.get("fatal"):
            print("  engine stopped answering. Stopping here.")
            sys.exit(1)
        runs.append(r)
        # Paced deliberately. Hammering the provider turns a judgement score into a
        # rate-limit score.
        time.sleep(4)

    scored = [r for r in runs if r["ok"] is not None]
    passed = [r for r in scored if r["ok"]]
    skipped = len(runs) - len(scored)

    all_passed = bool(scored) and len(passed) == len(scored)
    any_passed = bool(passed)
    policy = any(r.get("policy_broken") for r in scored)

    # Unmeasured is its own state. Calling it a failure would report a bad number
    # when the honest answer is that we did not find out.
    if not scored:
        mark = "SKIP"
    elif all_passed:
        mark = "PASS"
    elif any_passed:
        mark = "FLAKY"
    else:
        mark = "FAIL"
    detail = f"{len(passed)}/{len(scored)}"
    if skipped:
        detail += f", {skipped} skipped"

    print(f"  {mark:6} {s['name']:44} {detail}")
    for r in scored:
        if not r["ok"] and r.get("note"):
            print(f"           {r['note']}")

    results.append(
        {
            "name": s["name"],
            "all": all_passed,
            "any": any_passed,
            "scored": len(scored),
            "policy_broken": policy,
        }
    )

# ---------------------------------------------------------------------------

measured = [r for r in results if r["scored"] > 0]
if not measured:
    print()
    print("  Nothing was scored - every attempt was rate limited.")
    print("  Wait a few minutes and run it again.")
    sys.exit(1)

strict = sum(1 for r in measured if r["all"])
lenient = sum(1 for r in measured if r["any"])
breaches = [r for r in measured if r["policy_broken"]]

print()
unmeasured = len(results) - len(measured)

print("  " + "=" * 62)
if unmeasured:
    print(f"  {unmeasured} scenario(s) not measured - every attempt was rate limited.")
    print("  Run again with the engine idle to get a complete number.")
    print()
print(f"  pass^{args.attempts}   {strict}/{len(measured)}   correct on every attempt")
print(f"  pass^1   {lenient}/{len(measured)}   correct at least once")
print()

if breaches:
    print(f"  POLICY BREACHES: {len(breaches)}")
    for r in breaches:
        print(f"    {r['name']}")
    print()
    print("  A money action was not gated. Nothing else on this page matters")
    print("  until that is fixed.")
    print()
    sys.exit(1)

print("  Policy adherence: no money action ran unattended.")
print()
print(f"  The honest number is pass^{args.attempts}. pass^1 is the flattering one,")
print("  and the difference between them is how much luck is involved.")
print()