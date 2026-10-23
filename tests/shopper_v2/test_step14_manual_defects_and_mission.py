"""Step 14: the defects found in the October 19 manual browser session, and the
guided multi-product shopping mission.

Real graph, real Northfield (8001) / Kettle (8002) adapters, own sqlite file.
Everything below runs MODEL-FREE (no provider key; the model double is always
unavailable) except the checks that deliberately script ONE interpretation
claim to reproduce what a provider returned - those never reach a network.

Each observed defect has a check that fails on the pre-fix code:

  * "compare both" / "compare both the shoes showed above" after two shoes were
    shown asked for a second product instead of comparing them
  * "add Trailblazer Running Shoe" (and a misspelling of it) failed unless the
    product was searched for first
  * "I want jockey underwear" / "I want coffee" at a running shop returned
    unrelated products (a whole department) instead of saying it is not carried
  * "go back to the running shoes I saw earlier" produced a different, catalogue-
    wide list (or a comparison dead end) instead of restoring the earlier set
  * the long-run request showed only shoes; socks/energy needed another message

The mission checks drive the REAL cart through the existing add path
(fulfillment -> Decision Engine -> Risk Gate -> execution) and read it back from
the adapter: a stage advances only when the cart really holds the product.

    python tests/shopper_v2/test_step14_manual_defects_and_mission.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_NAME = "_test_shopper_v2_step14.db"
_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", _DB_NAME)
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///./{_DB_NAME}"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class DownClient:
    async def complete(self, **_):
        from engine.reasoning.llm import LLMUnavailable

        raise LLMUnavailable("test double: no model")


class ScriptedClient:
    """Answers the interpretation call with ONE scripted claim."""

    def __init__(self, claim: dict) -> None:
        self.claim = claim

    async def complete(self, *, system, messages, tools=None, force_tool=False):
        from engine.reasoning.llm import LLMResult, LLMUnavailable

        name = tools[0]["function"]["name"] if tools else None
        if name == "interpret_conversation":
            return LLMResult(text=None, model="scripted",
                             tool_calls=[{"name": "interpret_conversation", "arguments": self.claim}])
        raise LLMUnavailable(f"scripted: no model for {name}")


def claim(**kw) -> dict:
    base = {
        "capability": "discover", "context_action": "continue", "question_type": "retrieve",
        "reference_scope": "none", "reference_kind": "none", "conversational_repair": "none",
        "cart_operation": "none", "rationale": "scripted",
    }
    base.update(kw)
    return base


class Shopper:
    def __init__(self, engine, cid="conn_demo", *, cart=False, client=None, adapter=None):
        self.e, self.cid = engine, cid
        self.ad = adapter or engine.registry.adapter_for(cid)
        self.real = engine.registry.adapter_for(cid)
        self.sid = f"s14_{uuid.uuid4().hex[:10]}"
        self.cart_id = None
        self.want_cart = cart
        self.client = client

    async def start(self):
        if self.want_cart:
            self.cart_id = (await self.real.create_cart()).cart_id
        return self

    async def say(self, msg, *, variant=None, client=None):
        from engine.reasoning import ReasoningService
        from engine.shopper_v2.graph import run_turn_via_graph

        return await run_turn_via_graph(
            reasoning=ReasoningService(client or self.client or DownClient()), adapter=self.ad,
            session_id=self.sid, connection_id=self.cid, message=msg, cart_id=self.cart_id,
            decision=self.e.decision, gate=self.e.gate, execution=self.e.execution,
            selected_variant_id=variant,
        )

    async def cart(self):
        return await self.real.get_cart(self.cart_id)

    async def pending_option(self, index=0):
        from engine.db import shopping_state as store

        row = await store.get(self.sid, self.cid)
        queue = (row or {}).get("fulfillment_queue") or []
        item = next((q for q in queue if q.get("status") == "needs_variant"), None)
        return item["options"][index]["variant_id"] if item else None

    async def pending(self):
        from engine.db import cart_proposals as store

        return await store.pending_for_cart(self.cid, self.cart_id, self.sid)

    async def approve(self, proposal_id):
        """The shopper's own approval (what the Accept button calls)."""
        from engine.api.chat import accept_cart_proposal_core

        return await accept_cart_proposal_core(
            connection_id=self.cid, session_id=self.sid, cart_id=self.cart_id,
            proposal_id=proposal_id, shopper_id=None)

    async def tap_size(self, product_id, variant_id):
        """The shopper's own tap on a size button (the trusted-tap path)."""
        from engine.execution.trusted_action import run_trusted_action
        from shared.models import ActionType

        return await run_trusted_action(
            decision=self.e.decision, gate=self.e.gate, execution=self.e.execution,
            connection_id=self.cid, session_id=self.sid, cart_id=self.cart_id,
            action_type=ActionType.ADD_TO_CART,
            parameters={"chosen_product": product_id, "chosen_variant": variant_id, "said": "size"},
            rationale="shopper tapped a size")

    async def approve_latest(self, size_index=0):
        """Finish the shopper's pending add. Step 25 policy: an EXPLICIT add command executes, so what is usually
        pending is the SIZE question - answer it with the offered option (the shopper's own choice). If a proposal is
        pending instead (a non-explicit intent), approve it and tap its size as before. Returns the reply, or None
        when nothing was waiting (a size-free product was already added by the command itself)."""
        pend = await self.pending()
        if pend:
            reply = await self.approve(pend[-1]["proposal_id"])
            choices = getattr(reply, "choices", None) or []
            if choices:
                c = choices[size_index]
                await self.tap_size(c["product_id"], c["variant_id"])
            return reply
        option = await self.pending_option(size_index)
        if option:
            return await self.say("size", variant=option)
        return None

    async def mission_items(self):
        from engine.db import shopping_state as store

        row = await store.get(self.sid, self.cid)
        return (row or {}).get("mission_items") or []


def titles(r):
    return [p.title for p in r.products]


def cart_titles(cart):
    return [ln.title for ln in cart.lines]


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401  (registers its tables)
    from engine.shopper_v2 import guided_mission

    await create_schema()
    engine = build_engine()
    try:
        await run_checks(engine, guided_mission)
    finally:
        await engine.close()
        await dispose()


async def run_checks(engine, guided_mission) -> None:
    # ------------------------------------------------------------------ 1
    print("1. Comparing the pair that is on screen")
    s = await Shopper(engine).start()
    r = await s.say("show me running shoes under 5000")
    pair = sorted(titles(r))
    check("setup: two shoes under 5000 shown", len(pair) == 2)
    for phrase in ("compare both", "compare both the shoes showed above", "compare both shoes"):
        r = await s.say(phrase)
        check(f"{phrase!r} compares exactly the two shown (capability=compare)",
              r.trace.get("capability") == "compare" and sorted(titles(r)) == pair)
        check(f"{phrase!r} does not ask for a second product",
              "second" not in r.reply.lower() and "don't have two" not in r.reply.lower())

    s2 = await Shopper(engine, client=ScriptedClient(claim(
        capability="answer_question", question_type="relationship",
        reference_scope="last_rendered", reference_kind="none"))).start()
    await s2.say("show me running shoes under 5000", client=DownClient())
    r = await s2.say("compare both")
    check("a provider claim that leaves the plural pointer unresolved still ends in a comparison",
          r.trace.get("capability") == "compare" and len(r.products) == 2
          and "one side" not in r.reply.lower())

    s = await Shopper(engine).start()
    await s.say("show me all shoes available")
    await s.say("show me socks")
    r = await s.say("compare both the shoes showed above")
    check("after switching to socks, 'the shoes showed above' still compares the shoes",
          r.trace.get("capability") == "compare" and len(r.products) >= 2
          and all("sock" not in t.lower() for t in titles(r)))

    # ------------------------------------------------------------------ 2
    print("\n2. Naming a product is enough to add it")
    s = await Shopper(engine, cart=True).start()
    r = await s.say("add Trailblazer Running Shoe")
    check("exact product name, nothing searched first -> routed to a cart action",
          r.trace.get("capability") == "cart_action")
    check("an explicit add asks THAT product's size - no proposal waits for approval",
          await s.pending() == [] and await s.pending_option() is not None and "Trailblazer Running Shoe" in r.reply)
    check("nothing is in the cart until the shopper chooses the size", (await s.cart()).is_empty)
    await s.approve_latest()
    cart = await s.cart()
    check("the shopper's size choice lands exactly that product in the real cart",
          cart_titles(cart) == ["Trailblazer Running Shoe"])

    s = await Shopper(engine, cart=True).start()
    r = await s.say("add trailer blazzer shoe to the cart")
    check("a misspelled but unambiguous name resolves to the Trailblazer",
          r.trace.get("capability") == "cart_action" and "Trailblazer Running Shoe" in r.reply)

    s = await Shopper(engine, cart=True).start()
    await s.say("show me socks")
    r = await s.say("add Trailblazer Running Shoe")
    check("with socks in view, the named shoe (not the socks) is what gets added",
          "Trailblazer Running Shoe" in r.reply and "sock" not in r.reply.lower())

    s = await Shopper(engine, cart=True).start()
    await s.say("show me running shoes")
    r = await s.say("add the running shoe")
    check("an ambiguous description among several shoes is NOT silently resolved to one",
          (await s.cart()).is_empty and "Which one" in r.reply)

    s = await Shopper(engine, cart=True).start()
    r = await s.say("add Nonexistent Gizmo Pro")
    check("a name the shop does not sell adds nothing", (await s.cart()).is_empty)

    k = await Shopper(engine, "conn_kettle", cart=True).start()
    r = await k.say("add Colombia Huila Washed")
    check("Kettle: an exact product name resolves there too",
          r.trace.get("capability") == "cart_action" and "Colombia Huila Washed" in r.reply)

    # ------------------------------------------------------------------ 3
    print("\n3. A store that does not carry it says so")
    s = await Shopper(engine).start()
    await s.say("I want running shoes under 5000")
    for ask in ("I want jockey underwear", "I want coffee", "do you have laptops"):
        r = await s.say(ask)
        check(f"{ask!r}: no products shown", r.products == [])
        check(f"{ask!r}: says the store does not carry it",
              "doesn't seem to carry" in r.reply)
        check(f"{ask!r}: no degraded-processing disclaimer on a deterministic decision",
              "couldn't fully process" not in r.reply and "couldn't process that just now" not in r.reply)
    r = await s.say("compare both")
    check("the earlier running-shoe view survives the unmatched requests",
          r.trace.get("capability") == "compare" and len(r.products) == 2)

    from engine.shopper_v2 import vocabulary
    cats = vocabulary.cached_categories("conn_demo")
    check("setup: Northfield exposes a department list", bool(cats))
    apparel = next((c for c in cats if "apparel" in c.lower()), cats[0])
    sc = ScriptedClient(claim(catalog_categories=[apparel]))
    s = await Shopper(engine, client=sc).start()
    r = await s.say("I want jockey underwear")
    check("a provider that grounds 'underwear' onto a real department does not turn it into a browse of that department",
          r.products == [] and "doesn't seem to carry" in r.reply)

    k = await Shopper(engine, "conn_kettle").start()
    r = await k.say("I want coffee")
    check("Kettle (a coffee shop): 'coffee' still browses its catalogue", len(r.products) > 0)
    for ask in ("I need a fruity coffee", "I want a chocolatey blend", "do you have a decaf coffee"):
        r = await (await Shopper(engine, "conn_kettle").start()).say(ask)
        check(f"Kettle: {ask!r} (the shop's own domain word plus an unfamiliar one) is NOT declared uncarried",
              "doesn't seem to carry" not in r.reply)
    r = await (await Shopper(engine, "conn_kettle").start()).say("I want running shoes")
    check("Kettle: running shoes are honestly not carried", r.products == [] and "doesn't seem to carry" in r.reply)

    # ------------------------------------------------------------------ 4
    print("\n4. Going back restores what was shown")
    s = await Shopper(engine).start()
    first = await s.say("show me running shoes")
    first_titles = titles(first)
    await s.say("show me socks")
    r = await s.say("go back to the running shoes I saw earlier")
    check("not routed to compare", r.trace.get("capability") != "compare")
    check("the SAME products come back, in the same order", titles(r) == first_titles)
    s = await Shopper(engine).start()
    await s.say("show me running shoes under 5000")
    await s.say("show me socks")
    r = await s.say("go back to the running shoes")
    check("the budgeted pair comes back (constraint preserved)", len(r.products) == 2)
    r = await s.say("compare both")
    check("and 'compare both' then works on that pair",
          r.trace.get("capability") == "compare" and len(r.products) == 2)

    # ------------------------------------------------------------------ 5
    print("\n5. Reading a mission request")
    spec = guided_mission.parse_mission(
        "I am going for a long run. I want shoes, socks and something for energy. Suggest me which are available now")
    check("the long-run request reads as three stages",
          spec is not None and [x.label for x in spec.stages] == ["shoes", "socks", "energy"])
    check("with its activity", spec is not None and spec.activity == "running")
    spec = guided_mission.parse_mission("I need running shoes, socks and a hydration bottle")
    check("another phrasing reads too", spec is not None and [x.label for x in spec.stages] == [
        "running shoes", "socks", "hydration bottle"])
    for not_mission in ("show me shoes", "I want shoes", "I want to compare shoes and socks",
                        "add the shoes and the socks to my cart", "what is the difference between these and those"):
        check(f"{not_mission!r} is not a mission", guided_mission.parse_mission(not_mission) is None)

    # ------------------------------------------------------------------ 6
    print("\n6. The long-run mission, stage by stage")
    s = await Shopper(engine, cart=True).start()
    r = await s.say("I am going for a long run. I want shoes, socks and something for energy. "
                    "Suggest me which are available now")
    low = r.reply.lower()
    check("one reply covers all three categories", "shoes" in low and "socks" in low and "energy" in low)
    check("real prices from the shop are quoted", "INR" in r.reply)
    shown = titles(r)
    check("options for all three stages are shown up front",
          any("Shoe" in t for t in shown) and any("Sock" in t for t in shown) and any("Energy" in t for t in shown))
    check("it starts with the shoes and says what happens next", "start with" in low and "size" in low)
    items = await s.mission_items()
    check("three guided stages are tracked", len(items) == 3 and all(i["guided"] for i in items))
    check("nothing has been added yet", (await s.cart()).is_empty)
    check("every stage starts pending", [i["stage"] for i in items] == ["pending"] * 3)

    shoe = next(t for t in shown if "Trailblazer" in t) if any("Trailblazer" in t for t in shown) else \
        next(t for t in shown if "Shoe" in t)
    r = await s.say(f"add the {shoe}")
    check("choosing the shoe asks for its size (an explicit add executes; no approval step)",
          await s.pending() == [] and await s.pending_option() is not None and "size" in r.reply.lower())
    check("still nothing in the cart (never claimed before the cart confirms)", (await s.cart()).is_empty)
    check("the shoe stage is still pending", (await s.mission_items())[0]["stage"] == "pending")
    check("the reply does not claim an add", "confirmed in your cart" not in r.reply.lower())

    r = await s.approve_latest()
    cart = await s.cart()
    check("the size answer puts exactly one shoe line in the real cart", len(cart.lines) == 1 and cart.lines[0].quantity == 1)
    # Step 25: the size answer is the add itself, so the real cart is read (and confirmed) on THAT turn - the shopper
    # is told it was added, confirmed from the cart with its size, and moved on to socks in one reply.
    check("the size turn reports the add and confirms it from the cart (with its size)",
          r is not None and "Added" in r.reply and "Confirmed in your cart" in r.reply and shoe in r.reply)
    check("and advances to socks by itself", "socks" in r.reply.lower() and ("Next up" in r.reply or "Still to do" in r.reply))
    r = await s.say("what's next?")
    check("a later 'what's next' re-presents the open stage without re-confirming", "socks" in r.reply.lower() and "Confirmed in your cart" not in r.reply)
    check("the socks options are the cards now", any("Sock" in t for t in titles(r)))
    items = await s.mission_items()
    check("stage 1 is recorded as added from the real cart line",
          items[0]["stage"] == "added" and items[0]["added_product_id"] == cart.lines[0].product_id)
    check("stages 2 and 3 are still pending", [i["stage"] for i in items[1:]] == ["pending", "pending"])

    r = await s.say("add the Trailblazer Running Shoe" if "Trailblazer" in shoe else f"add the {shoe}")
    cart = await s.cart()
    check("repeating the add cannot duplicate the line", len(cart.lines) == 1 and cart.lines[0].quantity == 1)
    check("and the retry is answered honestly (no new proposal)", "already" in r.reply.lower() and await s.pending() == [])

    # interruption in the middle of the mission, then resume
    r = await s.say("what's your return policy?")
    items_after = await s.mission_items()
    check("an unrelated aside leaves the mission state intact",
          [i["stage"] for i in items_after] == ["added", "pending", "pending"])
    r = await s.say("what's next?")
    check("'what's next' re-presents the open stage (socks), not the whole thing again",
          "socks" in r.reply.lower() and "Sock" in "".join(titles(r)))
    check("and does not repeat the confirmed stage as a question", "start with" not in r.reply.lower())

    r = await s.say("add the Performance Socks Three Pack")
    cart = await s.cart()
    check("socks (no size to choose) are added by the command itself: nothing pending, two lines now",
          await s.pending() == [] and len(cart.lines) == 2 and any("Sock" in t for t in cart_titles(cart)))
    await s.approve_latest()
    cart = await s.cart()
    check("socks are in the real cart exactly once", sum("Sock" in t for t in cart_titles(cart)) == 1)
    r = await s.say("what's next?")
    check("energy is opened next", "energy" in r.reply.lower() and "Energy Gel" in " ".join(titles(r)))
    r = await s.say("add the Energy Gel Box of 12")
    await s.approve_latest()
    cart = await s.cart()
    r = await s.say("what's next?")
    check("energy reaches the real cart", any("Energy" in t for t in cart_titles(cart)))
    check("three stages, three lines, quantity one each", len(cart.lines) == 3 and all(l.quantity == 1 for l in cart.lines))
    low = r.reply.lower()
    check("the closing summary names every selection", all(
        any(w in ln.title.lower() for w in (ln.title.lower(),)) and ln.title in r.reply for ln in cart.lines))
    check("it states the real cart total", str(cart.grand_total or cart.subtotal) in r.reply)
    check("it shows the chosen size", "size 8" in low or "size" in low)
    check("it says nothing is left", "nothing left" in low or "completes" in low)
    items = await s.mission_items()
    check("all stages are added", [i["stage"] for i in items] == ["added"] * 3)
    r = await s.say("what's next?")
    check("after completion 'what's next' summarizes instead of re-asking", "completes" in r.reply.lower())
    r = await s.say("what about waterproof jackets?")
    check("'what about <something else>' after completion is a fresh question, not the mission",
          "completes" not in r.reply.lower())

    # ------------------------------------------------------------------ 7
    print("\n7. Changes of mind")
    s = await Shopper(engine, cart=True).start()
    await s.say("I want running shoes, socks and something for energy")
    r = await s.say("skip the shoes")
    items = await s.mission_items()
    check("skipping a stage records it and moves on", items[0]["stage"] == "skipped" and "socks" in r.reply.lower())
    check("skipping never touches the cart", (await s.cart()).is_empty)
    r = await s.say("skip")
    r = await s.say("skip")
    check("skipping everything ends with an honest summary",
          "completes" in r.reply.lower() and "Skipped" in r.reply and (await s.cart()).is_empty)

    s = await Shopper(engine, cart=True).start()
    await s.say("I want running shoes, socks and something for energy")
    r = await s.say("keep the shoes under 3000")
    check("a budget change re-filters the stage", r.products and all(
        float(p.price.amount) <= 3000 for p in r.products))
    items = await s.mission_items()
    check("other stages are untouched by it", all(i["stage"] == "pending" for i in items)
          and items[1]["candidates"] and items[2]["candidates"])
    before = (await s.mission_items())[0]["candidates"]
    r = await s.say("keep the shoes under 100")
    check("an impossible budget is reported honestly and the earlier options kept",
          "don't have any" in r.reply and (await s.mission_items())[0]["candidates"] == before)

    # correct a mistaken selection by adding the other product; the stage stays satisfied
    s = await Shopper(engine, cart=True).start()
    await s.say("I want running shoes, socks and something for energy")
    await s.say("add the Starter Road Running Shoe")
    await s.approve_latest()
    r = await s.say("add the Trailblazer Running Shoe")
    await s.approve_latest()
    cart = await s.cart()
    check("choosing a different shoe afterwards is a normal add and the mission state stays consistent",
          len(cart.lines) == 2 and (await s.mission_items())[0]["stage"] == "added")

    # a tap through the card path (not a chat turn): the cart is still the authority
    s = await Shopper(engine, cart=True).start()
    await s.say("I want running shoes, socks and something for energy")
    prod = await s.real.search_products("Performance Socks")
    sock = next(p for p in prod.products if "Sock" in p.title)
    shoe_res = await s.real.search_products("Trailblazer")
    shoe_p = next(p for p in shoe_res.products if "Trailblazer" in p.title)
    adv = await guided_mission.advance_from_cart(
        adapter=s.real, session_id=s.sid, connection_id=s.cid, cart_id=s.cart_id)
    check("nothing in the cart -> nothing advances", adv is None)
    await s.real.add_to_cart(s.cart_id, shoe_p.product_id, variant_id=shoe_p.variants[0].variant_id)
    adv = await guided_mission.advance_from_cart(
        adapter=s.real, session_id=s.sid, connection_id=s.cid, cart_id=s.cart_id)
    check("a card-tap add is picked up from the cart and opens the socks stage",
          adv is not None and "Confirmed in your cart" in adv[0] and "socks" in adv[0].lower())
    adv = await guided_mission.advance_from_cart(
        adapter=s.real, session_id=s.sid, connection_id=s.cid, cart_id=s.cart_id)
    check("running the sync again is idempotent", adv is None)

    # ------------------------------------------------------------------ 8
    print("\n8. Stock and platform conditions")
    flat = await s.real.search_products("Studio Training Flat")
    flat_p = next((p for p in flat.products if "Studio Training Flat" in p.title), None)
    if flat_p is not None and not guided_mission._in_stock(flat_p):
        s = await Shopper(engine, cart=True).start()
        r = await s.say("I need studio training flat, socks and something for energy")
        check("a sold-out-only stage is reported as unavailable, not substituted",
              "nothing in stock" in r.reply.lower())
        items = await s.mission_items()
        check("it is skipped and the next stage becomes the open one",
              items[0]["stage"] == "unavailable" and "socks" in r.reply.lower())
    else:
        check("(sold-out fixture product not present - sold-out stage check skipped)", True)

    class DownAdapter:
        def __init__(self, real):
            self._r = real

        async def search_products(self, *a, **k):
            raise RuntimeError("platform down")

        def __getattr__(self, name):
            return getattr(self._r, name)

    s = await Shopper(engine, cart=True, adapter=DownAdapter(engine.registry.adapter_for("conn_demo"))).start()
    r = await s.say("I want running shoes, socks and something for energy")
    check("platform down: the turn degrades with a reply, no crash", isinstance(r.reply, str) and r.reply)
    check("platform down: no mission state is invented", await s.mission_items() == [])
    check("platform down: no products are invented", r.products == [])

    # provider-found missions become guided too
    from engine.reasoning.service import MissionItemIntent
    from engine.shopper_v2.missions import run_mission_turn
    from engine.shopper_v2.state import ShoppingStateV2

    class _ScriptedReasoning:
        enabled = True

        async def interpret_mission_items(self, **kwargs):
            return MissionItemIntent(new_item_labels=["running shoes", "socks", "energy gel"])

    st = ShoppingStateV2(session_id=f"s14_{uuid.uuid4().hex[:8]}", connection_id="conn_demo")
    ad = engine.registry.adapter_for("conn_demo")
    reply, products, new_state, handled, _ids = await run_mission_turn(
        reasoning=_ScriptedReasoning(), adapter=ad,
        message="I need running shoes, socks and energy gel", history=None, state=st)
    check("a model-found three-item mission is presented as a guided plan",
          handled and len([i for i in new_state.mission_items if i.guided]) == 3 and "start with" in reply.lower())

    # authority: a stage never advances without a cart line
    items = [i.model_copy() for i in new_state.mission_items]
    class _EmptyCart:
        lines = []
    check("sync with an empty cart completes nothing", guided_mission.sync_items_with_cart(items, _EmptyCart()) == [])
    check("sync with no readable cart completes nothing", guided_mission.sync_items_with_cart(items, None) == [])

    print()
    print(f"  {failures} FAILURE(S)" if failures else "  all Step 14 checks passed")


asyncio.run(main())
sys.exit(1 if failures else 0)
