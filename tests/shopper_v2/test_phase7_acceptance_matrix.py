"""Phase 0-5 production acceptance matrix - scripted-model, full real stack.

Every scenario drives the REAL graph (`run_turn_via_graph`): real Focus
Stack/LastRendered/state persistence, real topic transitions, real
retrieval + normalization + evidence + hard filters, real adapters over
HTTP (Northfield REST 8001, Kettle GraphQL 8002), real cart. The ONLY
scripted thing is the model's CLAIM (`interpret_conversation` tool output)
- so what is under test is everything the application does with a claim,
deterministically and repeatably, independent of provider throttling.
Provider-side classification quality is measured separately (paced,
real-provider runs); this file proves the application behaves correctly
GIVEN each classification the model can produce, including "the model
was unavailable" (a `None` claim).

Each scenario runs REPS times in fresh sessions; a scenario passes only if
every repetition passes.

Requires Northfield (8001) and Kettle (8002). Own isolated sqlite file.

    python tests/shopper_v2/test_phase7_acceptance_matrix.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase7.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_shopper_v2_phase7.db"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

REPS = int(os.environ.get("ACCEPTANCE_REPS", "3"))

results: dict[str, list[bool]] = {}
details: dict[str, list[str]] = {}


def record(scenario: str, label: str, ok: bool) -> None:
    results.setdefault(scenario, []).append(ok)
    if not ok:
        details.setdefault(scenario, []).append(label)


def claim(**kw) -> dict:
    base = {
        "capability": "discover", "context_action": "continue", "question_type": "retrieve",
        "reference_scope": "none", "reference_kind": "none", "conversational_repair": "none",
        "cart_operation": "none", "rationale": "scripted",
    }
    base.update(kw)
    return base


class ScriptedClient:
    """A provider double: interpretation calls pop the next scripted claim
    (`None` = the provider failed this turn); every other model call
    (synthesis, judges) is unavailable, forcing the deterministic paths."""

    def __init__(self) -> None:
        self.queue: list[dict | None] = []
        self.interpret_calls = 0

    async def complete(self, *, system, messages, tools=None, force_tool=False):
        from engine.reasoning.llm import LLMResult, LLMUnavailable

        name = tools[0]["function"]["name"] if tools else None
        if name == "interpret_conversation":
            self.interpret_calls += 1
            if not self.queue:
                raise LLMUnavailable("scripted: no claim queued")
            item = self.queue.pop(0)
            if item is None:
                raise LLMUnavailable("scripted provider failure")
            return LLMResult(
                text=None, model="scripted",
                tool_calls=[{"name": "interpret_conversation", "arguments": item}],
            )
        raise LLMUnavailable(f"scripted: no model for {name}")


class Session:
    def __init__(self, engine, connection_id: str) -> None:
        from engine.reasoning import ReasoningService

        self.engine = engine
        self.cid = connection_id
        self.sid = f"acc_{uuid.uuid4().hex[:10]}"
        self.client = ScriptedClient()
        self.reasoning = ReasoningService(self.client)
        self.adapter = engine.registry.adapter_for(connection_id)
        self.cart_id: str | None = None

    async def with_cart(self) -> "Session":
        self.cart_id = (await self.adapter.create_cart()).cart_id
        return self

    async def say(self, message: str, model_claim: dict | None):
        from engine.shopper_v2.graph import run_turn_via_graph

        self.client.queue.append(model_claim)
        return await run_turn_via_graph(
            reasoning=self.reasoning, adapter=self.adapter, session_id=self.sid,
            connection_id=self.cid, message=message, cart_id=self.cart_id,
            decision=self.engine.decision, gate=self.engine.gate, execution=self.engine.execution,
        )

    async def state(self):
        from engine.shopper_v2.state import load

        return await load(self.sid, self.cid)

    async def cart_lines(self) -> list[str]:
        if not self.cart_id:
            return []
        cart = await self.adapter.get_cart(self.cart_id)
        return [line.product_id for line in cart.lines]


def titles(result) -> list[str]:
    return [p.title for p in result.products]


# ---------------------------------------------------------------- scenarios

async def s_comparison_then_recommendation(eng) -> None:
    """D/E/F/G: recommendation after a comparison operates on exactly the
    compared pair - never a fresh search, never a broader set."""
    name = "E/F/G recommendation from the established comparison set"
    s = Session(eng, "conn_demo")
    r1 = await s.say("show me running shoes under 5000", claim(context_action="push"))
    pair = set(titles(r1))
    record(name, "search under 5000 verifies exactly the two in-budget shoes",
           pair == {"Starter Road Running Shoe", "Trailblazer Running Shoe"})
    r2 = await s.say("compare both", claim(
        capability="compare", question_type="relationship",
        reference_scope="last_rendered", reference_kind="all"))
    record(name, "comparison names both products",
           all(t in r2.reply for t in pair) or set(titles(r2)) == pair)
    for phrase in ("Recommend any one", "which one do you recommend?", "which would you choose?"):
        r3 = await s.say(phrase, claim(question_type="recommendation"))
        picked = titles(r3)
        record(name, f"{phrase!r}: exactly one product, from the compared pair",
               len(picked) == 1 and picked[0] in pair)
        record(name, f"{phrase!r}: never 'couldn't find anything matching that search'",
               "couldn't find anything" not in r3.reply)
    st = await s.state()
    record(name, "LastRendered agrees with the recommended product",
           st.last_rendered is not None and len(st.last_rendered.product_ids) == 1)


async def s_recommendation_narrowed_view(eng) -> None:
    """A wide topic (9 shoes), a comparison of the first two, then "recommend
    one": the recommendation must stay inside the compared two. This is the
    shape that distinguishes 'operate on what is on screen' from 'operate on
    the whole active topic'."""
    name = "E/F recommendation stays inside a narrowed comparison"
    s = Session(eng, "conn_demo")
    r1 = await s.say("show me running shoes", claim(context_action="push"))
    first_two = titles(r1)[:2]
    r2 = await s.say("compare the first two", claim(
        capability="compare", question_type="retrieve",
        reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1, 2]))
    record(name, "comparison is exactly the first two shown", sorted(titles(r2)) == sorted(first_two))
    for phrase in ("recommend one", "which is better for my use?"):
        r3 = await s.say(phrase, claim(question_type="recommendation"))
        record(name, f"{phrase!r}: the pick is one of the two compared",
               len(r3.products) == 1 and r3.products[0].title in first_two)


async def s_recommendation_provider_down(eng) -> None:
    """The reported sequence with the model UNAVAILABLE for the follow-up
    (the provider's own rate limit). Two honest outcomes, never the
    discovery no-match reply and never an invented pick:
      - phrasings the existing model-free detector recognises get a grounded
        deterministic pick from exactly the compared pair;
      - anything else says plainly that the message was not processed and
        keeps the shopper's established view."""
    name = "E/T recommendation after comparison, provider down"
    for phrase, expect_pick in (
        ("which one do you recommend?", True),
        ("what would you recommend?", True),
        ("Recommend any one", True),   # Step 22: a bare recommendation imperative is recognised (was: not processed)
        ("recommend one", True),
        ("which would you choose?", False),
    ):
        s = Session(eng, "conn_demo")
        r1 = await s.say("show me running shoes under 5000", claim(context_action="push"))
        pair = set(titles(r1))
        await s.say("compare both", claim(
            capability="compare", question_type="retrieve",
            reference_scope="last_rendered", reference_kind="all"))
        before = await s.state()
        r3 = await s.say(phrase, None)      # interpretation fails this turn
        record(name, f"{phrase!r}: never the discovery no-match reply",
               "couldn't find anything matching" not in r3.reply)
        if expect_pick:
            record(name, f"{phrase!r}: a single grounded pick from the pair",
                   len(r3.products) == 1 and r3.products[0].title in pair)
        else:
            record(name, f"{phrase!r}: says it was not processed and names the current view",
                   "couldn't process" in r3.reply.lower() and all(t in r3.reply for t in pair))
            record(name, f"{phrase!r}: invents no product", r3.products == [])
        after = await s.state()
        record(name, f"{phrase!r}: the established view survives the failed turn",
               after.last_rendered is not None
               and before.last_rendered is not None
               and (expect_pick or after.last_rendered.product_ids == before.last_rendered.product_ids))


async def s_reference_never_leaks(eng) -> None:
    """H/I: 'first two' binds to what is on screen, never another topic."""
    name = "H/I relative references bind to the rendered set"
    for scope, kind in (("last_rendered", "ordinal"), ("none", "ordinal"), ("named", "all")):
        s = Session(eng, "conn_demo")
        await s.say("show me socks", claim(context_action="push"))
        r2 = await s.say("show me waterproof trail shoes", claim(context_action="push"))
        record(name, f"[{scope}/{kind}] waterproof search shows the Highridge",
               titles(r2) == ["Highridge Waterproof Trail Running Shoe"])
        r3 = await s.say("Compare the first two", claim(
            capability="compare", question_type="relationship",
            reference_scope=scope, reference_kind=kind, reference_positions=[1, 2]))
        record(name, f"[{scope}/{kind}] never pairs the shoe with the socks from an earlier topic",
               "Sock" not in r3.reply and all("Sock" not in t for t in titles(r3)))
        record(name, f"[{scope}/{kind}] honest: asks for the missing second product",
               "two" in r3.reply.lower() or "other product" in r3.reply.lower())


async def s_topic_switch_and_resume(eng) -> None:
    """B/C: switching, pausing, resuming - views never bleed."""
    name = "B/C topic switch + resume, no leakage"
    s = Session(eng, "conn_demo")
    r1 = await s.say("show me running shoes", claim(context_action="push"))
    record(name, "shoes shown", len(r1.products) >= 5)
    r2 = await s.say("show me socks", claim(context_action="push"))
    record(name, "socks shown", titles(r2) == ["Performance Socks Three Pack"])
    r3 = await s.say("go back to the running shoes", claim(context_action="resume"))
    st = await s.state()
    active = next((t for t in st.focus_stack if t.state == "active"), None)
    record(name, "resume reactivates the shoe topic", active is not None and len(active.product_ids) >= 5)
    r4 = await s.say("which is the first one", claim(
        capability="discover", question_type="retrieve",
        reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1]))
    record(name, "'the first one' after resume is a shoe, not the sock",
           all("Sock" not in t for t in titles(r4)) and all("Sock" not in r4.reply for _ in [0]))


async def s_spelling_northfield(eng) -> None:
    """J/L: typos resolve against real catalog vocabulary and are verified."""
    name = "J/L spelling on Northfield"
    s = Session(eng, "conn_demo")
    r1 = await s.say("wateproof trail shoes", claim(context_action="push"))
    record(name, "'wateproof' -> waterproof: exactly the waterproof trail shoe",
           titles(r1) == ["Highridge Waterproof Trail Running Shoe"])
    s = Session(eng, "conn_demo")
    r2 = await s.say("runnning shoes", claim(context_action="push"))
    record(name, "'runnning' -> running: real running shoes", len(r2.products) >= 5
           and all("Running" in t or "Shoe" in t for t in titles(r2)))
    s = Session(eng, "conn_demo")
    r3 = await s.say("lightwieght running shoes", claim(context_action="push"))
    record(name, "'lightwieght' -> lightweight: only the lightweight shoes",
           set(titles(r3)) == {"Trailblazer Running Shoe", "Featherline Speed Running Shoe"})
    s = Session(eng, "conn_demo")
    r4 = await s.say("show me kayaks", claim(context_action="push"))
    record(name, "a non-catalog word is NOT force-corrected into a product",
           titles(r4) == [] and "couldn't find" in r4.reply.lower() or "carry" in r4.reply.lower())


async def s_kettle_vocabulary(eng) -> None:
    """K/M: generic category vocabulary on the merchant whose taxonomy does
    not contain the shopper's word."""
    name = "K/M Kettle vocabulary"
    beans = {"Ethiopia Guji Natural", "Colombia Huila Washed", "Brazil Cerrado Pulped Natural",
             "Morning Bell Espresso Blend", "Nightshift Dark Roast", "House Filter Blend",
             "Swiss Water Decaf"}
    s = Session(eng, "conn_kettle")
    r = await s.say("show me coffee blends", claim(context_action="push"))
    record(name, "'coffee blends' -> all four blends (domain word neutralized)",
           set(titles(r)) == {"Morning Bell Espresso Blend", "Nightshift Dark Roast",
                              "House Filter Blend", "Swiss Water Decaf"})
    s = Session(eng, "conn_kettle")
    r = await s.say("cofffee", claim(context_action="push", catalog_categories=["Single origin", "Blends"]))
    record(name, "'cofffee' + grounded categories -> the bean products only", set(titles(r)) == beans)
    s = Session(eng, "conn_kettle")
    r = await s.say("coffee beans", claim(context_action="push", catalog_categories=["single origin", "BLENDS"]))
    record(name, "'coffee beans' grounded onto real categories (any case)", set(titles(r)) == beans)
    # Step 24: "beans" is now grounded deterministically from variant options ("250g whole bean"; see
    # test_step24_variant_grounding.py), so the no-grounding / invented-category probes use a word with NO
    # catalogue evidence anywhere. The properties asserted are unchanged.
    s = Session(eng, "conn_kettle")
    r = await s.say("flibbertigibbet", claim(context_action="push", catalog_categories=["Invented Category"]))
    record(name, "an INVENTED category is rejected - no fabricated results", titles(r) == [])
    s = Session(eng, "conn_kettle")
    r = await s.say("flibbertigibbet", claim(context_action="push"))
    record(name, "a word with no grounding: honest no-match, nothing invented", titles(r) == [])
    s = Session(eng, "conn_kettle")
    r = await s.say("coffee", claim(context_action="push"))
    # The merchant declares its coffee range (departments Single origin / Blends), so a
    # bare shop-domain word is a real browse of exactly the in-stock coffees - never an
    # empty result, and never equipment, gifts or subscriptions.
    record(name, "'coffee' alone (no model grounding): a real browse of the coffee range only",
           set(titles(r)) == beans)
    s = Session(eng, "conn_kettle")
    r = await s.say("decaf", claim(context_action="push"))
    record(name, "'decaf' -> Swiss Water Decaf", titles(r) == ["Swiss Water Decaf"])
    s = Session(eng, "conn_kettle")
    r = await s.say("filter coffee", claim(context_action="push"))
    record(name, "'filter coffee' -> House Filter Blend", titles(r) == ["House Filter Blend"])


async def s_constraints_and_budget(eng) -> None:
    """N/O: a budget states once and persists across turns and operations."""
    name = "N/O constraints + budget persistence"
    s = Session(eng, "conn_demo")
    await s.say("show me running shoes under 5000", claim(context_action="push"))
    st = await s.state()
    record(name, "budget captured from the message", st.budget_amount in ("5000", "5000.0"))
    r2 = await s.say("show me running shoes", claim(context_action="push"))
    record(name, "budget persists into a later turn that never repeats it",
           set(titles(r2)) <= {"Starter Road Running Shoe", "Trailblazer Running Shoe"} and len(titles(r2)) >= 1)
    r3 = await s.say("recommend one", claim(question_type="recommendation"))
    record(name, "recommendation respects the persisted budget",
           len(r3.products) == 1 and float(r3.products[0].price.amount) <= 5000)
    s2 = Session(eng, "conn_demo")
    await s2.say("My budget is 5000", claim(capability="conversational"))
    st2 = await s2.state()
    record(name, "a direct budget statement is captured", st2.budget_amount in ("5000", "5000.0"))


async def s_preference_lifecycle(eng) -> None:
    """P: stated preference persists; cancellation removes it and leaves a tombstone."""
    name = "P preference persistence + cancellation"
    s = Session(eng, "conn_demo")
    await s.say("I prefer lightweight shoes", claim(
        context_action="push", shopping_state={"nice_to_have_add": ["lightweight"]}))
    st = await s.state()
    record(name, "preference stored", "lightweight" in st.nice_to_have)
    r2 = await s.say("no, forget that", claim(
        capability="conversational", conversational_repair="cancellation"))
    st2 = await s.state()
    record(name, "cancellation removes the preference", "lightweight" not in st2.nice_to_have)
    record(name, "cancellation leaves a tombstone", "lightweight" in st2.cancelled_preferences)
    record(name, "cancellation reply is honest", "no problem" in r2.reply.lower())


async def s_compound_turns(eng) -> None:
    """Q: state change + search + comparison in one message, any primary class."""
    name = "Q compound turns"
    for primary in ("discover", "conversational", "unknown"):
        s = Session(eng, "conn_demo")
        r = await s.say("show me running shoes under 5000 and compare the best two", claim(
            capability=primary, context_action="push",
            secondary_operation={"capability": "compare", "reference_names": []}))
        record(name, f"[primary={primary}] secondary comparison executes",
               "Comparing" in r.reply or "compar" in r.reply.lower())
        record(name, f"[primary={primary}] over-budget product never compared",
               "Featherline" not in r.reply and "6299" not in r.reply)


async def s_cart_followups(eng) -> None:
    """R: 'add it' after a recommendation targets the recommended product;
    remove/clear reach the real cart operation; ambiguity clarifies."""
    name = "R cart follow-ups"
    s = await Session(eng, "conn_demo").with_cart()
    await s.say("show me running shoes under 5000", claim(context_action="push"))
    r2 = await s.say("recommend one", claim(question_type="recommendation"))
    picked = r2.products[0].product_id if r2.products else None
    r3 = await s.say("add it", claim(
        capability="cart_action", cart_operation="add",
        reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1]))
    # Contract (Step 25): an explicit add executes. The recommended product is a sized shoe, so "add it" asks THAT
    # product's size first (no proposal, nothing in the cart until the shopper chooses).
    from engine.db import cart_proposals as proposals_store

    waiting = await proposals_store.pending_for_cart("conn_demo", s.cart_id, s.sid)
    lines = await s.cart_lines()
    asked = (r3.trace or {}).get("fulfillment_pending") or {}
    record(name, "'add it' targets exactly the recommended product (asks its size)",
           picked is not None and asked.get("product_id") == picked)
    record(name, "no proposal was created and nothing is in the cart until the size is chosen", not waiting and not lines)

    s2 = await Session(eng, "conn_demo").with_cart()
    await s2.adapter.add_to_cart(s2.cart_id, "P3002", quantity=1)   # Performance Socks
    await s2.adapter.add_to_cart(s2.cart_id, "P3003", quantity=1)   # Foam Roller
    r4 = await s2.say("clear my cart", claim(capability="cart_action", cart_operation="clear"))
    record(name, "'clear my cart' reaches the clear operation (a proposal, not discovery)",
           "clear" in r4.reply.lower() and (r4.trace or {}).get("cart_proposal_id"))
    s3 = await Session(eng, "conn_demo").with_cart()
    await s3.adapter.add_to_cart(s3.cart_id, "P3002", quantity=1)
    await s3.adapter.add_to_cart(s3.cart_id, "P3003", quantity=1)
    r5 = await s3.say("remove it", claim(
        capability="cart_action", cart_operation="remove",
        reference_scope="cart", reference_kind="named", reference_names=["thing that matches nothing"]))
    record(name, "an ambiguous cart reference asks, and removes nothing",
           len(await s3.cart_lines()) == 2)


async def s_no_match_and_provider_failure(eng) -> None:
    """S/T: honest no-match; provider failure never fabricates state."""
    name = "S/T honest no-match + provider failure"
    s = Session(eng, "conn_demo")
    r = await s.say("show me kayaks", claim(context_action="push"))
    record(name, "no-match is honest and shows nothing", r.products == [] and "kayak" not in " ".join(titles(r)).lower())
    record(name, "a genuine no-match also says what the store really carries",
           "This store carries:" in r.reply and "Footwear" in r.reply)
    sk = Session(eng, "conn_kettle")
    rk = await sk.say("flibbertigibbet", claim(context_action="push"))
    record(name, "Kettle no-match lists Kettle's real categories (not Northfield's)",
           "Blends" in rk.reply and "Footwear" not in rk.reply)

    s = await Session(eng, "conn_demo").with_cart()
    r1 = await s.say("show me running shoes under 5000", None)      # provider down
    record(name, "[provider down] a plain search still works deterministically", len(r1.products) == 2)
    before = await s.state()
    r2 = await s.say("recommend one", None)
    record(name, "[provider down] recommend: no crash, no fabricated product",
           all(p.title in ("Starter Road Running Shoe", "Trailblazer Running Shoe") for p in r2.products))
    record(name, "[provider down] recommend: the failure is disclosed, not read as a search miss",
           r2.products != [] or "couldn't process" in r2.reply.lower()
           or "couldn't fully process" in r2.reply.lower())
    r3 = await s.say("add it to my cart", None)
    record(name, "[provider down] 'add it' never mutates the cart", await s.cart_lines() == [])
    r4 = await s.say("compare both", None)
    record(name, "[provider down] compare never invents a pairing from another topic",
           all("Sock" not in t for t in titles(r4)))
    after = await s.state()
    record(name, "[provider down] no false preference/topic state was written",
           after.nice_to_have == before.nice_to_have and after.priorities == before.priorities)


SCENARIOS = [
    s_comparison_then_recommendation, s_recommendation_narrowed_view, s_recommendation_provider_down, s_reference_never_leaks, s_topic_switch_and_resume,
    s_spelling_northfield, s_kettle_vocabulary, s_constraints_and_budget,
    s_preference_lifecycle, s_compound_turns, s_cart_followups, s_no_match_and_provider_failure,
]


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401 - registers the V2 tables before create_schema

    await create_schema()
    engine = build_engine()
    try:
        for rep in range(REPS):
            for scenario in SCENARIOS:
                try:
                    await scenario(engine)
                except Exception as exc:  # noqa: BLE001 - a crash is a failure, reported
                    record(scenario.__name__, f"CRASH {type(exc).__name__}: {exc}", False)
    finally:
        await engine.close()
        await dispose()

    print()
    total_fail = 0
    for scenario, oks in results.items():
        bad = oks.count(False)
        total_fail += bad
        print(f"  {'ok    ' if bad == 0 else 'FAILED'}  {scenario}  ({len(oks) - bad}/{len(oks)} checks over {REPS} reps)")
        for d in sorted(set(details.get(scenario, []))):
            print(f"            - {d}")
    print()
    print(f"  {'ALL ACCEPTANCE CHECKS PASSED' if total_fail == 0 else f'{total_fail} CHECK(S) FAILED'}")
    print()
    sys.exit(1 if total_fail else 0)


if __name__ == "__main__":
    asyncio.run(main())
