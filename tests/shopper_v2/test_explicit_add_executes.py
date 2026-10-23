"""An explicit, unambiguous typed command to add a resolved product executes the add - and nothing else does.

Policy change (Step 25, deliberate, replacing the Step 17 contract for this one case): the shopper typed
"add it to the cart" and was shown a proposal they then had to approve (user-reported 2026-10-21). An explicit,
unambiguous command to add a RESOLVED product now runs the deterministic cart-add flow directly and reports what
the platform actually did. The mutation path is unchanged and still authoritative: the turn creates the same
PROPOSE_CART_CHANGE record (the audit trail) and runs it through `accept_cart_proposal_core` - stock/price
re-check against live data, the atomic claim (idempotency), Decision -> Risk Gate -> Execution -> Adapter.

Stays a proposal / a question / a no-op:
  recommendations and discovery           never add, never propose-and-run
  "it" with several referents             asks which one
  a product that needs a size             asks the size first, then adds the shopper's own choice
  vague agreement ("sounds good")         never an add
  advice questions / negations            "should I add it?", "don't add it" never add
  platform failure                        never reported as success; the cart is unchanged
  an unreadable cart                      falls back to the safe proposal (a retry could double-add)
  a repeated or concurrent command        quantity stays one
  a proposal made by something else       typed "yes" still approves exactly that one

Model unavailable for every turn (deterministic path). Needs Northfield (8001) and Kettle (8002) running.

    python tests/shopper_v2/test_explicit_add_executes.py
"""
from __future__ import annotations

import os
import sys
import uuid

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_explicit_add.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_explicit_add.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


class Down:
    async def complete(self, **kw):
        from engine.reasoning.llm import LLMUnavailable

        raise LLMUnavailable("no model in this test")


class Shopper:
    def __init__(self, eng, cid="conn_demo"):
        from engine.reasoning import ReasoningService

        self.eng, self.cid = eng, cid
        self.sid = f"ea_{uuid.uuid4().hex[:8]}"
        self.ad = eng.registry.adapter_for(cid)
        self.rs = ReasoningService(Down())
        self.cart_id = None

    async def start(self):
        self.cart_id = (await self.ad.create_cart()).cart_id
        return self

    async def say(self, message, variant=None):
        from engine.shopper_v2.graph import run_turn_via_graph

        return await run_turn_via_graph(
            reasoning=self.rs, adapter=self.ad, session_id=self.sid, connection_id=self.cid, message=message,
            cart_id=self.cart_id, decision=self.eng.decision, gate=self.eng.gate, execution=self.eng.execution,
            selected_variant_id=variant)

    async def lines(self):
        cart = await self.ad.get_cart(self.cart_id)
        return [(l.product_id, l.quantity, getattr(l, "variant_id", None)) for l in cart.lines]

    async def pending(self):
        from engine import db

        return await db.cart_proposals.pending_for_cart(self.cid, self.cart_id, self.sid)


def titles(r):
    return [p.title for p in r.products]


def first_size(r):
    p = (r.trace or {}).get("fulfillment_pending") or {}
    opts = p.get("options") or []
    return opts[0]["variant_id"] if opts else None


async def checks(eng):
    SOCKS, GEL = "P3002", None
    print("1. 'add it to the cart' for a resolved, size-free product executes directly")
    s = await Shopper(eng).start()
    await s.say("show me socks")
    r = await s.say("add it to the cart")
    check("the product is in the real cart, quantity one", [(p, q) for p, q, _ in await s.lines()] == [(SOCKS, 1)], await s.lines())
    check("no proposal is left waiting for approval", await s.pending() == [], await s.pending())
    check("the reply says it was added (from the platform's actual result), not 'confirm to go ahead'",
          "added" in r.reply.lower() and "confirm to go ahead" not in r.reply.lower() and "proposal" not in r.reply.lower(), r.reply[:200])
    check("the turn tells the storefront the cart changed", r.trace.get("cart_changed") is True, r.trace.get("cart_changed"))

    print("\n2. A named product, no prior view")
    s = await Shopper(eng).start()
    r = await s.say("add Performance Socks Three Pack")
    check("added directly", [(p, q) for p, q, _ in await s.lines()] == [(SOCKS, 1)] and await s.pending() == [], (await s.lines(), r.reply[:120]))

    print("\n3. A repeated command never doubles the line")
    r = await s.say("add Performance Socks Three Pack")
    check("still quantity one", [(p, q) for p, q, _ in await s.lines()] == [(SOCKS, 1)], await s.lines())
    check("told it is already there", "already" in r.reply.lower(), r.reply[:140])

    print("\n4. Concurrent duplicate commands (double-click / double Enter) add once")
    s = await Shopper(eng).start()
    await s.say("show me socks")
    await asyncio.gather(s.say("add it to the cart"), s.say("add it to the cart"), s.say("add it to the cart"))
    check("quantity is exactly one", [(p, q) for p, q, _ in await s.lines()] == [(SOCKS, 1)], await s.lines())

    print("\n5. A product that needs a size asks first, then adds the shopper's OWN choice")
    s = await Shopper(eng).start()
    await s.say("show me the trailblazer")
    r = await s.say("add it to the cart")
    size = first_size(r)
    check("it asks for the size and adds nothing yet", size is not None and await s.lines() == [], (size, r.reply[:120]))
    r = await s.say("7", variant=size)
    lines = await s.lines()
    check("the tapped size is added, exactly once", len(lines) == 1 and lines[0][1] == 1 and lines[0][2] == size, lines)
    check("and no proposal is pending", await s.pending() == [])

    print("\n6. Ambiguous 'it' asks which; recommendations and vague agreement never add")
    s = await Shopper(eng).start()
    await s.say("show me running shoes under 5000")
    r = await s.say("add it to the cart")
    check("two products in view: it asks which one, nothing added", await s.lines() == [] and await s.pending() == [] and "which" in r.reply.lower(), r.reply[:140])
    await s.say("recommend one")
    check("a recommendation adds nothing and proposes nothing", await s.lines() == [] and await s.pending() == [])
    for vague in ("sounds good", "ok", "I'll take it", "nice", "yes", "perfect"):
        await s.say(vague)
    check("vague agreement never adds or proposes", await s.lines() == [] and await s.pending() == [], (await s.lines(), await s.pending()))

    print("\n7. Questions and negations about adding are not commands")
    s = await Shopper(eng).start()
    await s.say("show me socks")
    for msg in ("should I add it to the cart?", "don't add it to the cart", "do not add it", "never add it", "how do I add it to my cart?",
                "what happens if I add it?", "if it is cheap, add it"):
        await s.say(msg)
    check("none of them changed the cart or created a proposal", await s.lines() == [] and await s.pending() == [], (await s.lines(), await s.pending()))

    print("\n8. Platform failure is reported honestly")
    from shared.models.errors import CommerceError, ErrorCode

    s = await Shopper(eng).start()
    await s.say("show me socks")
    # The accept core executes through the engine's OWN adapter instance (`engine.api.deps.engine`), not the test's:
    # inject the platform failure where execution actually runs.
    from engine.api import deps

    adapter = deps.engine.registry.adapter_for(s.cid)
    original = adapter.add_to_cart

    async def broken(*a, **kw):
        raise CommerceError(ErrorCode.UPSTREAM_ERROR, "platform exploded", platform_detail={"secret": "x"})

    adapter.add_to_cart = broken
    try:
        r = await s.say("add it to the cart")
    finally:
        adapter.add_to_cart = original
    check("no success is claimed", "added" not in r.reply.lower().replace("couldn't add", ""), r.reply[:200])
    check("the cart is unchanged", await s.lines() == [], await s.lines())
    check("no raw platform error reaches the shopper", "exploded" not in r.reply and "secret" not in r.reply and "UPSTREAM" not in r.reply, r.reply[:200])
    check("the storefront is not told the cart changed", not r.trace.get("cart_changed"), r.trace.get("cart_changed"))

    print("\n9. An unreadable cart falls back to the safe proposal (a retried command could double-add)")
    s = await Shopper(eng).start()
    await s.say("show me socks")
    real_get = s.ad.get_cart

    async def unreadable(*a, **kw):
        raise CommerceError(ErrorCode.TIMEOUT, "slow")

    s.ad.get_cart = unreadable
    try:
        r = await s.say("add it to the cart")
    finally:
        s.ad.get_cart = real_get
    check("nothing was added; a proposal awaits the shopper's approval instead",
          await s.lines() == [] and len(await s.pending()) == 1, (await s.lines(), r.reply[:140]))

    print("\n10. A proposal made by something else is still approved by typing 'yes'")
    from engine import db

    s = await Shopper(eng).start()
    prop = await db.cart_proposals.create(connection_id=s.cid, session_id=s.sid, shopper_id=None, cart_id=s.cart_id,
                                          change_type="ADD", product_id=SOCKS, reason="goes with your cart",
                                          evidence=["declared complementary by the merchant"], snapshot_price=None)
    r = await s.say("yes")
    check("'yes' approves exactly that proposal", [(p, q) for p, q, _ in await s.lines()] == [(SOCKS, 1)] and await s.pending() == [], (await s.lines(), r.reply[:120]))

    print("\n11. Stock is still enforced")
    s = await Shopper(eng, "conn_kettle").start()
    r = await s.say("add Kenya Nyeri AA")
    check("a sold-out product is not added", await s.lines() == [], await s.lines())
    check("and is not reported as added", "added" not in r.reply.lower().replace("couldn't add", ""), r.reply[:160])

    print("\n12. 'add both' adds the compared products one at a time, each with the shopper's own size")
    s = await Shopper(eng).start()
    r1 = await s.say("show me running shoes under 5000")
    pair = [p.product_id for p in r1.products]
    await s.say("compare both")
    r = await s.say("add both to the cart")
    for _ in range(4):
        size = first_size(r)
        if size is None:
            break
        r = await s.say("pick", variant=size)
    got = sorted(p for p, _, _ in await s.lines())
    check("both compared products are in the cart, one line each", got == sorted(pair) and all(q == 1 for _, q, _ in await s.lines()), (got, pair, r.reply[:140]))
    check("no proposals are left pending", await s.pending() == [], await s.pending())


async def review_checks(eng):
    from engine import db
    from engine.shopper_v2.add_command import classify_add_message, is_explicit_add_command

    print("\n13. Classifier guards (review findings): deferral, capability questions, quoted text never execute")
    for msg in ("I might add it later", "I'll add it later", "add it tomorrow", "maybe add it", "perhaps add it to my cart",
                "can I add it to my cart?", "could I add it?", "may I add it", "is it possible to add it",
                'the sign says "add to cart"', 'press "buy" to continue', "you cannot add it", "I can't add it"):
        check(f"not a command: {msg!r}", not is_explicit_add_command(msg), classify_add_message(msg))
    for msg in ("add it to the cart", "add it", "please add it to my cart", "can you add it to my cart?", "could you please add it",
                "add both", "buy it", "put it in my cart", "add the Trailblazer Running Shoe"):
        check(f"still a command: {msg!r}", is_explicit_add_command(msg), classify_add_message(msg))

    print("\n14. A stale explicit size question is dropped, never executed on a much later answer")
    from datetime import UTC, datetime, timedelta

    from engine.db import shopping_state as store

    s = await Shopper(eng).start()
    r = await s.say("add Trailblazer Running Shoe")
    size = first_size(r)
    check("setup: the size question is open", size is not None and await s.lines() == [])
    row = await store.get(s.sid, s.cid)
    queue = list((row or {}).get("fulfillment_queue") or [])
    for item in queue:
        item["asked_at"] = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    await store.merge(s.sid, s.cid, fulfillment_queue=queue)
    await s.say("7", variant=size)
    check("three hours later the answer does NOT add anything", await s.lines() == [], await s.lines())

    print("\n15. An explicit command never executes an older proposal with different details")
    s = await Shopper(eng).start()
    stale = await db.cart_proposals.create(connection_id=s.cid, session_id=s.sid, shopper_id=None, cart_id=s.cart_id,
                                           change_type="ADD", product_id="P3002", quantity=3, reason="older suggestion",
                                           evidence=["declared complementary by the merchant"], snapshot_price=None)
    r = await s.say("add Performance Socks Three Pack")
    lines = await s.lines()
    check("exactly what was just said is added (quantity one), not the older quantity-three proposal",
          [(p, q) for p, q, _ in lines] == [("P3002", 1)], lines)
    old = await db.cart_proposals.get(s.cid, stale["proposal_id"])
    check("the older proposal was retired, not left pending", old["state"] != "PENDING", old["state"])
    check("nothing is left pending", await s.pending() == [])

    print("\n16. An unexpected error AFTER the platform write is reported from the real cart")
    s = await Shopper(eng).start()
    await s.say("show me socks")
    original = db.cart_proposals.set_state
    calls = {"n": 0}

    async def failing_set_state(*a, **kw):
        # let the claim-time bookkeeping through, fail the final state write (after the cart was changed)
        if kw.get("state") == "EXECUTED":
            raise RuntimeError("bookkeeping failed after the platform write")
        return await original(*a, **kw)

    db.cart_proposals.set_state = failing_set_state
    try:
        r = await s.say("add it to the cart")
    finally:
        db.cart_proposals.set_state = original
    lines = await s.lines()
    check("the product really is in the cart (the write happened)", [(p, q) for p, q, _ in lines] == [("P3002", 1)], lines)
    check("and the reply says so - it does not claim the cart is unchanged", "unchanged" not in r.reply.lower() and "added" in r.reply.lower(), r.reply[:200])
    check("no raw error text", "bookkeeping" not in r.reply and "RuntimeError" not in r.reply, r.reply[:200])


async def ambiguity_checks(eng):
    from types import SimpleNamespace

    from engine.shopper_v2 import graph
    from engine.shopper_v2.add_command import reference_is_ambiguous

    print("\n17. Ambiguity policy (pure): a guess among several products never authorizes an add")
    two = ["Trailblazer Running Shoe", "Cloudfoam Trail Runner"]
    check("bare 'add it' with 3 in view is ambiguous", reference_is_ambiguous("add it", ["Trailblazer Running Shoe"], 3))
    check("the only product in view is not ambiguous", not reference_is_ambiguous("add it", ["Trailblazer Running Shoe"], 1, True))
    check("a pick that is NOT the only product in view and is not named is ambiguous",
          reference_is_ambiguous("add the socks", ["Trailblazer Running Shoe"], 1, False))
    check("nothing in view and a bare 'add it' is ambiguous", reference_is_ambiguous("add it", ["Trailblazer Running Shoe"], 0, False))
    check("naming the product is not ambiguous", not reference_is_ambiguous("add the Trailblazer", ["Trailblazer Running Shoe"], 3))
    check("a positional pointer is not ambiguous", not reference_is_ambiguous("add the second one", ["Cloudfoam Trail Runner"], 3))
    check("'add both' is not ambiguous", not reference_is_ambiguous("add both", two, 2))
    check("two ids from a bare 'add it' is ambiguous", reference_is_ambiguous("add it", two, 2))

    print("\n18. Ambiguity policy (pipeline): a model pick of one product among several must ask, not add")
    s = await Shopper(eng).start()
    shown = await s.say("show me running shoes")
    ids = [p.product_id for p in (shown.products or [])]
    if len(ids) < 2:
        check("setup: at least two products shown", False, ids)
        return
    orig_interpret, orig_verify = graph.interpret_turn, graph.verify

    async def fake_interpret(**kw):
        return SimpleNamespace()

    def fake_verify(claim, context, products, message):
        return SimpleNamespace(cart_operation="add", needs_clarification=False,
                               resolved_reference=SimpleNamespace(product_ids=[ids[0]]))

    graph.interpret_turn, graph.verify = fake_interpret, fake_verify
    try:
        r = await s.say("add it to the cart")
    finally:
        graph.interpret_turn, graph.verify = orig_interpret, orig_verify
    check("nothing was added to the cart", await s.lines() == [], await s.lines())
    check("the shopper is asked which one", "which" in r.reply.lower(), r.reply[:200])
    check("no proposal is left pending", await s.pending() == [], await s.pending())


async def main():
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401 - registers the V2 tables before create_schema

    await create_schema()
    eng = build_engine()
    try:
        await checks(eng)
        await review_checks(eng)
        await ambiguity_checks(eng)
    finally:
        await eng.close()
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
