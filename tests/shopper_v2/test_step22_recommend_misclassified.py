"""A bare imperative recommendation ("recommend one") when the MODEL misclassifies it as a new search.

Live-reproduced in the Step 22 provider-backed acceptance run (OpenAI gpt-4o-mini, 3 of 3 times in the
long flow): after "show me running shoes", "show me socks", "go back to the running shoes", "compare
the first two", the message "recommend one" was interpreted as a discovery search for the word
"recommend", found nothing, and the shopper was told "I couldn't find anything matching that search".
The reference backstop that rescues "which one do you recommend?" never fired because the closed-class
recommendation vocabulary did not recognise a message whose only content is a recommendation verb.

The provider is a scripted double (the same shape as test_phase7_acceptance_matrix.py): interpretation
returns a plain discovery claim, exactly what the live model returned. Everything else is the real
pipeline over the real sample merchants.

    python tests/shopper_v2/test_step22_recommend_misclassified.py
"""
from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_step22_recommend.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_step22_recommend.db"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


def claim(**kw) -> dict:
    base = {
        "capability": "discover", "context_action": "continue", "question_type": "retrieve",
        "reference_scope": "none", "reference_kind": "none", "conversational_repair": "none",
        "cart_operation": "none", "rationale": "scripted",
    }
    base.update(kw)
    return base


class ScriptedClient:
    def __init__(self) -> None:
        self.queue: list[dict | None] = []

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
        self.sid = f"s22_{uuid.uuid4().hex[:10]}"
        self.client = ScriptedClient()
        self.reasoning = ReasoningService(self.client)
        self.adapter = engine.registry.adapter_for(connection_id)
        self.cart_id = None

    async def with_cart(self):
        self.cart_id = (await self.adapter.create_cart()).cart_id
        return self

    async def say(self, message, model_claim):
        from engine.shopper_v2.graph import run_turn_via_graph

        self.client.queue.append(model_claim)
        return await run_turn_via_graph(
            reasoning=self.reasoning, adapter=self.adapter, session_id=self.sid, connection_id=self.cid,
            message=message, cart_id=self.cart_id, decision=self.engine.decision, gate=self.engine.gate,
            execution=self.engine.execution)

    async def cart_lines(self):
        if not self.cart_id:
            return []
        return [l.product_id for l in (await self.adapter.get_cart(self.cart_id)).lines]


def titles(r):
    return [p.title for p in r.products]


COMPARE = dict(capability="compare", question_type="retrieve", reference_scope="last_rendered",
               reference_kind="ordinal", reference_positions=[1, 2])


async def scenarios(eng):
    print("1. The live long flow, model misclassifies 'recommend one' as a discovery search")
    for phrase in ("recommend one", "Recommend any one", "recommend one please", "recommend one of them"):
        s = await Session(eng, "conn_demo").with_cart()
        await s.say("show me running shoes", claim(context_action="push"))
        await s.say("show me socks", claim(context_action="push"))
        await s.say("go back to the running shoes", claim(context_action="push"))
        r4 = await s.say("compare the first two", claim(**COMPARE))
        pair = titles(r4)
        r5 = await s.say(phrase, claim())  # the model's plain discovery claim, as observed live
        check(f"{phrase!r}: never the discovery 'couldn't find anything' reply", "couldn't find anything" not in r5.reply)
        check(f"{phrase!r}: exactly one grounded pick, from the compared pair",
              len(pair) == 2 and len(r5.products) == 1 and r5.products[0].title in pair)
        check(f"{phrase!r}: the cart is untouched (a recommendation never adds)", await s.cart_lines() == [])

    print("\n2. Short flow (search under budget, then the bare request) with a misclassifying model")
    s = await Session(eng, "conn_demo").with_cart()
    r1 = await s.say("show me running shoes under 5000", claim(context_action="push"))
    r2 = await s.say("recommend one", claim())
    check("one pick, from what was just shown",
          len(r2.products) == 1 and r2.products[0].title in titles(r1) and "couldn't find anything" not in r2.reply)

    print("\n3. Provider down (no interpretation at all): the same grounded pick, no fabrication")
    s = Session(eng, "conn_demo")
    r1 = await s.say("show me running shoes under 5000", claim(context_action="push"))
    await s.say("compare both", claim(capability="compare", question_type="retrieve",
                                       reference_scope="last_rendered", reference_kind="all"))
    r3 = await s.say("recommend one", None)
    check("a single pick from the pair, not the discovery no-match reply",
          len(r3.products) == 1 and r3.products[0].title in titles(r1) and "couldn't find anything" not in r3.reply)

    print("\n4. Pure unit checks of the detector (a request WITH content is never a bare recommendation)")
    from engine.shopper_v2.capability import looks_like_recommendation_request as f
    for m, want in (("recommend one", True), ("Recommend any one", True), ("recommend one please", True),
                    ("suggest one", True), ("pick one", True), ("what do you recommend?", True),
                    ("recommend a waterproof trail shoe", False), ("recommend socks", False),
                    ("show me running shoes", False), ("I recommend nothing", False), ("one", False),
                    ("add one", False)):
        check(f"looks_like_recommendation_request({m!r}) is {want}", f(m) is want)


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
