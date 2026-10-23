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


def _claim(**kw):
    base = {"capability": "discover", "context_action": "continue", "question_type": "retrieve",
            "reference_scope": "none", "reference_kind": "none", "conversational_repair": "none",
            "cart_operation": "none", "rationale": "scripted"}
    base.update(kw)
    return base


class ScriptedModel:
    """Interpretation calls pop the next scripted claim; every other model call is unavailable."""

    def __init__(self):
        self.queue = []

    async def complete(self, *, system, messages, tools=None, force_tool=False):
        from engine.reasoning.llm import LLMResult, LLMUnavailable

        name = tools[0]["function"]["name"] if tools else None
        if name == "interpret_conversation" and self.queue:
            return LLMResult(text=None, model="scripted",
                             tool_calls=[{"name": "interpret_conversation", "arguments": self.queue.pop(0)}])
        raise LLMUnavailable("scripted: nothing queued")


async def ambiguity_checks(eng):
    from engine.reasoning import ReasoningService
    from engine.shopper_v2.add_command import classify_add_message, reference_is_ambiguous
    from engine.shopper_v2.graph import run_turn_via_graph

    A = ("P1001", "Trailblazer Running Shoe", 8999)
    B = ("P1009", "Starter Road Running Shoe", 4999)
    C = ("P1010", "Cloudstep Max Cushion Running Shoe", 12999)
    K1, K2 = ("K1", "Kettle Blend Coffee", 500), ("K2", "Ethiopia Coffee", 700)
    amb = reference_is_ambiguous

    print("\n17. Ambiguity policy (pure): the model's pick is checked against the shopper's words")
    check("bare 'add it' with two in view asks", amb("add it", [A], [A, B]))
    check("generic title words ('running shoe') do not identify a pick", amb("add the running shoe", [A], [A, B]))
    check("a distinguishing word names it", not amb("add the Trailblazer", [A], [A, B]))
    check("'the coffee' with two coffees in view asks", amb("add the coffee", [K1], [K1, K2]))
    check("'the Kettle coffee' names it", not amb("add the Kettle coffee", [K1], [K1, K2]))
    check("the only product in view needs no name", not amb("add it", [A], [A]))
    check("a pick that is not in view and not named asks", amb("add it", [A], [B]))
    check("'the second one' - pick IS second", not amb("add the second one", [B], [A, B]))
    check("'the second one' - model picked the first: asks", amb("add the second one", [A], [A, B]))
    check("'the first one' - pick IS first", not amb("add the first one", [A], [A, B]))
    check("'the first one' - model picked the second: asks", amb("add the first one", [B], [A, B]))
    check("'the last one' - pick IS last", not amb("add the last one", [C], [A, B, C]))
    check("'the last one' - model picked another: asks", amb("add the last one", [A], [A, B, C]))
    check("'the cheapest' - pick IS cheapest", not amb("add the cheapest", [B], [A, B, C]))
    check("'the cheapest' - model picked another: asks", amb("add the cheapest", [A], [A, B, C]))
    check("'the cheaper one' cannot be verified: asks", amb("add the cheaper one", [B], [A, B]))
    check("'both' - pick is exactly the two in view", not amb("add both", [A, B], [A, B]))
    check("'both' with three in view asks", amb("add both", [A, B], [A, B, C]))
    check("'them' with three in view and a two-product pick asks", amb("add them", [A, B], [A, B, C]))
    check("'all of them' - pick is the whole view", not amb("add all of them", [A, B, C], [A, B, C]))
    check("'the first two' - pick IS the first two", not amb("add the first two", [A, B], [A, B, C]))
    check("'the first two' - model picked others: asks", amb("add the first two", [B, C], [A, B, C]))
    check("'and' alone does not waive the check", amb("add the shoes and I'll check out", [A, B], [A, B, C]))
    check("two products each named", not amb("add the Trailblazer and the Starter Road", [A, B], [A, B, C]))
    check("nothing chosen asks", amb("add it", [], [A, B]))
    S3, D, E = ("S3", "Merino Wool Sock", 300), ("P1011", "Featherline Speed Running Shoe", 9999), ("P1012", "Highridge Trail Shoe", 7999)
    check("a pick NOT in view is not 'named' by a shared title word ('socks')", amb("add the socks", [S3], [A, B], [A, B], frozenset()))
    check("...unless the catalogue itself confirmed that exact product", not amb("add the Merino Wool Sock", [S3], [A, B], [A, B], frozenset({"S3"})))
    check("an empty view and a generic word asks", amb("add the coffee", [K1], [], [], frozenset()))
    check("'all of them' means what is ON SCREEN, not the hidden active topic",
          amb("add all of them", [A, B, C, D, E], [A, B, C], [A, B, C, D, E]))
    check("'all of them' with the pick equal to the screen is fine", not amb("add all of them", [A, B, C], [A, B, C], [A, B, C, D, E]))
    check("'add 2 of the trail shoes' is a quantity, not 'both products'", amb("add 2 of the trail shoes", [A, B], [A, B]))
    check("'the top rated one' is not a position", amb("add the top rated one", [A], [A, B]))

    print("\n17b. Classifier: advice and quotation never execute")
    for msg in ("do you recommend I buy the Trailblazer?", "you think I should add it?", "any reason to buy the Trailblazer",
                "the button says 'add to cart'", "add it?", "is it worth buying the Trailblazer"):
        check(f"not a command: {msg!r}", classify_add_message(msg) != "command", classify_add_message(msg))
    for msg in ("would you buy the Trailblazer", "do you want me to add the Trailblazer", "would you add it to my cart",
                "tell me how to add it to my cart", "explain why I should buy it"):
        check(f"a question without a '?' is not a command: {msg!r}", classify_add_message(msg) != "command", classify_add_message(msg))
    for msg in ("would you buy it?", "would you add this?", "tell me what happens when I add it", "after I add it, do I get a receipt?",
                "can you add it?"):
        check(f"not a command (opinion / hypothetical / bare request-as-question): {msg!r}", classify_add_message(msg) != "command", classify_add_message(msg))
    for msg in ("can you add it to my cart?", "could you please add it", "add the Trailblazer", "I'll add it, it's fine", "please buy the socks"):
        check(f"still a command: {msg!r}", classify_add_message(msg) == "command", classify_add_message(msg))

    print("\n18. Ambiguity policy (pipeline): scripted model picks resolve through the real graph")
    model = ScriptedModel()
    ad = eng.registry.adapter_for("conn_demo")

    async def session():
        cart = (await ad.create_cart()).cart_id
        sid = f"amb_{uuid.uuid4().hex[:8]}"

        async def say(message, claim=None, variant=None):
            if claim is not None:
                model.queue.append(claim)
            return await run_turn_via_graph(
                reasoning=ReasoningService(model), adapter=ad, session_id=sid, connection_id="conn_demo", message=message,
                cart_id=cart, decision=eng.decision, gate=eng.gate, execution=eng.execution, selected_variant_id=variant)

        async def lines():
            return [(ln.product_id, ln.quantity) for ln in (await ad.get_cart(cart)).lines]
        return say, lines

    say, lines = await session()
    shown = await say("show me running shoes", _claim(context_action="push"))
    check("setup: several shoes in view", len(shown.products or []) >= 2, [p.title for p in (shown.products or [])])
    r = await say("add the running shoe", _claim(capability="cart_action", cart_operation="add", reference_scope="last_rendered",
                                                  reference_kind="ordinal", reference_positions=[1]))
    check("generic words + a model pick: asks which one", "which one" in r.reply.lower(), r.reply[:160])
    check("it did not go on to ask a size for the model's pick", "size" not in r.reply.lower(), r.reply[:160])
    check("nothing added, nothing pending", await lines() == [])
    r = await say("add the Trailblazer Running Shoe", _claim(capability="cart_action", cart_operation="add",
                                                              reference_scope="last_rendered", reference_kind="ordinal",
                                                              reference_positions=[1]))
    check("when the shopper names the product the guard lets it through (to its size question)",
          "size" in r.reply.lower() and "which one" not in r.reply.lower(), r.reply[:160])
    check("a sized product is still not added before the size is chosen", await lines() == [])
    r = await say("should I add the Trailblazer Running Shoe?", _claim(capability="cart_action", cart_operation="add",
                                                                         reference_scope="last_rendered", reference_kind="ordinal",
                                                                         reference_positions=[1]))
    check("a question never adds, whatever the model claims", await lines() == [], await lines())
    r = await say("what if I added the Trailblazer Running Shoe", _claim(capability="cart_action", cart_operation="add",
                                                                           reference_scope="last_rendered", reference_kind="ordinal",
                                                                           reference_positions=[1]))
    check("a hypothetical never adds", await lines() == [], await lines())
    r = await say("add it", _claim(capability="cart_action", cart_operation="add", reference_scope="last_rendered",
                                    reference_kind="ordinal", reference_positions=[1]))
    check("bare 'add it' with several shoes in view asks", "which one" in r.reply.lower() and await lines() == [], r.reply[:160])
    r = await say("add the second one", _claim(capability="cart_action", cart_operation="add", reference_scope="last_rendered",
                                                reference_kind="ordinal", reference_positions=[1]))
    check("'the second one' but the model picked the first: asks, adds nothing", "which one" in r.reply.lower() and await lines() == [],
          r.reply[:160])


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
