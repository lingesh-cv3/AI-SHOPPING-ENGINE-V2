"""A category word with ZERO retrieval candidates must not be answered with a manufactured single product.

Live-reproduced in the Step 22 provider-backed run (gpt-4o-mini, 2 of 2): on Kettle & Bloom, "beans"
matches no product text (lexical and semantic retrieval both returned 0 candidates) and the model did not
supply `catalog_categories`; the semantic fallback judge then browsed a generic catalogue sample and
picked ONE coffee ("I found one that fits: Colombia Huila Washed") for a request that names a whole class
of products (the category holds seven coffees). The fix: the judge only ever chooses among REAL retrieved
candidates; with none, there is nothing to judge and the shopper gets the honest no-match reply that lists
the store's real categories. The grounded path is unchanged: a model-supplied `catalog_categories`
(re-validated against the store's real taxonomy) still returns that category's real, in-stock products.

The provider is a scripted double: interpretation returns the claims the live model produced, and the
judge methods return a pick when (wrongly) asked - so a manufactured pick would show up as a product.

    python tests/shopper_v2/test_step23_zero_candidate_judge.py   (needs Northfield on 8001, Kettle on 8002)
"""
from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_step23_zero_candidate.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_step23_zero_candidate.db"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool, got: object = None) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def claim(**kw) -> dict:
    base = {
        "capability": "discover", "context_action": "push", "question_type": "retrieve",
        "reference_scope": "none", "reference_kind": "none", "conversational_repair": "none",
        "cart_operation": "none", "rationale": "scripted",
    }
    base.update(kw)
    return base


class ScriptedClient:
    """Interpretation: the scripted claim. Judges: ALWAYS willing to pick the first candidate offered, so any
    call that reaches them with a catalogue sample would manufacture a product. `judge_calls` counts them."""

    def __init__(self) -> None:
        self.queue: list[dict | None] = []
        self.judge_calls = 0

    async def complete(self, *, system, messages, tools=None, force_tool=False):
        from engine.reasoning.llm import LLMResult, LLMUnavailable

        name = tools[0]["function"]["name"] if tools else None
        if name == "interpret_conversation":
            if not self.queue:
                raise LLMUnavailable("scripted: no claim queued")
            item = self.queue.pop(0)
            if item is None:
                raise LLMUnavailable("scripted provider failure")
            return LLMResult(text=None, model="scripted",
                             tool_calls=[{"name": "interpret_conversation", "arguments": item}])
        raise LLMUnavailable(f"scripted: no model for {name}")


class Session:
    def __init__(self, engine, connection_id: str) -> None:
        from engine.reasoning import ReasoningService

        self.engine, self.cid = engine, connection_id
        self.sid = f"s23z_{uuid.uuid4().hex[:10]}"
        self.client = ScriptedClient()
        self.reasoning = ReasoningService(self.client)
        self.adapter = engine.registry.adapter_for(connection_id)
        outer = self

        async def judge_relevance(*, need, categories, sample_titles):
            outer.client.judge_calls += 1
            return True

        async def judge_best_match(*, need, candidates):
            outer.client.judge_calls += 1
            return candidates[0]["product_id"] if candidates else None

        self.reasoning.judge_relevance = judge_relevance
        self.reasoning.judge_best_match = judge_best_match
        # the judge fallback only runs for an `enabled` reasoning service
        try:
            self.reasoning.enabled = True
        except Exception:  # noqa: BLE001
            pass

    async def say(self, message, model_claim):
        from engine.shopper_v2.graph import run_turn_via_graph

        self.client.queue.append(model_claim)
        return await run_turn_via_graph(
            reasoning=self.reasoning, adapter=self.adapter, session_id=self.sid, connection_id=self.cid,
            message=message, cart_id=None, decision=self.engine.decision, gate=self.engine.gate,
            execution=self.engine.execution)


def titles(r):
    return [p.title for p in r.products]


BEANS = {"Ethiopia Guji Natural", "Colombia Huila Washed", "Brazil Cerrado Pulped Natural",
         "Morning Bell Espresso Blend", "Nightshift Dark Roast", "House Filter Blend", "Swiss Water Decaf"}


async def scenarios(eng):
    # NOTE (Step 24): "beans" itself is now grounded deterministically in variant options (see
    # test_step24_variant_grounding.py); these cases use a word with NO catalogue evidence anywhere.
    print("1. A word with no catalogue evidence on Kettle: zero candidates, no category mapping from the model")
    s = Session(eng, "conn_kettle")
    check("setup: the scripted judge is willing to pick (so a pick would be manufactured)",
          getattr(s.reasoning, "enabled", False) is True)
    r = await s.say("flibbertigibbet", claim())
    check("no product is manufactured", titles(r) == [], titles(r))
    check("the reply is the honest no-match / clarification listing the store's REAL categories",
          "couldn't find anything" in r.reply and "Blends" in r.reply and "Footwear" not in r.reply, r.reply[:200])
    check("the fallback judge never browsed a catalogue sample for a pick", s.client.judge_calls == 0, s.client.judge_calls)

    print("\n2. The grounded category path still works (model-supplied catalog_categories, re-validated)")
    s = Session(eng, "conn_kettle")
    r = await s.say("flibbertigibbet", claim(catalog_categories=["Single origin", "Blends"]))
    check("unknown word + real categories -> the real in-stock products of those categories",
          len(titles(r)) >= 4 and set(titles(r)) <= BEANS, titles(r))
    s = Session(eng, "conn_kettle")
    r = await s.say("flibbertigibbet", claim(catalog_categories=["Footwear"]))
    check("a category the store does not have (invented) is rejected: nothing manufactured",
          titles(r) == [] and "Footwear" not in " ".join(titles(r)), (titles(r), r.reply[:120]))

    print("\n3. Supported categories / names by plain retrieval are unaffected")
    for q, want in (("show me coffee", BEANS), ("coffee blends", {"Morning Bell Espresso Blend", "Nightshift Dark Roast",
                                                                "House Filter Blend", "Swiss Water Decaf"})):
        s = Session(eng, "conn_kettle")
        r = await s.say(q, claim())
        check(f"{q!r} -> its real products", set(titles(r)) == want, titles(r))
    s = Session(eng, "conn_kettle")
    r = await s.say("Swiss Water Decaf", claim())
    check("a valid single-product result still returns that one product", titles(r) == ["Swiss Water Decaf"], titles(r))

    print("\n4. A zero-candidate request on Northfield (a different catalogue) behaves the same")
    s = Session(eng, "conn_demo")
    r = await s.say("zzzqx widgets", claim())
    check("nonsense word: no product manufactured, judge never asked to pick", titles(r) == [] and s.client.judge_calls == 0,
          (titles(r), s.client.judge_calls))
    check("the reply lists Northfield's categories, not Kettle's", "Footwear" in r.reply and "Blends" not in r.reply, r.reply[:160])

    print("\n5. Candidates that exist but are not confident: the judge may still choose among REAL candidates only")
    s = Session(eng, "conn_demo")
    r = await s.say("show me running shoes", claim())
    check("an ordinary search is unaffected", len(titles(r)) >= 2, titles(r))

    print("\n6. Empty catalogue: no crash, no manufactured product, honest reply")

    class EmptyAdapter:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        async def search_products(self, *a, **kw):
            class R:
                products: list = []
                total_available = 0
            return R()

    s = Session(eng, "conn_kettle")
    s.adapter = EmptyAdapter(s.adapter)
    r = await s.say("flibbertigibbet", claim())
    check("empty catalogue: no products, judge never asked to pick, a reply is still given",
          titles(r) == [] and s.client.judge_calls == 0 and bool(r.reply), (titles(r), s.client.judge_calls, r.reply[:100]))


async def main():
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401 - registers the V2 tables before create_schema

    await create_schema()
    eng = build_engine()
    try:
        await scenarios(eng)
    finally:
        await eng.close()
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
