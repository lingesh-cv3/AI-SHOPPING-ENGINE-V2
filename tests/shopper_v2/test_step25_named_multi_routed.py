"""Two (or three) products NAMED in one message are resolved from the catalogue even when the model routes the
message to the compare/relationship answer.

Live finding (Step 25, gpt-4o-mini): "Compare the Trailblazer Running Shoe and the Performance Socks Three Pack" was
claimed as capability=compare / question_type=relationship, and the relationship answer (which needs attributes on
both sides) replied "I can see one side of that ... which other product or group did you mean?". The scripted claim
below is that shape. Needs Northfield (8001) running.

    python tests/shopper_v2/test_step25_named_multi_routed.py
"""
from __future__ import annotations

import os
import sys
import uuid

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_named_multi.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_named_multi.db"
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


RELATIONSHIP_CLAIM = {
    "capability": "compare", "context_action": "continue", "question_type": "relationship",
    "reference_scope": "none", "reference_kind": "none", "conversational_repair": "none",
    "cart_operation": "none", "rationale": "scripted: the live model's reading of 'compare A and B'",
}


class Scripted:
    def __init__(self):
        self.calls = 0

    async def complete(self, *, system, messages, tools=None, force_tool=False):
        from engine.reasoning.llm import LLMResult, LLMUnavailable

        name = tools[0]["function"]["name"] if tools else None
        if name == "interpret_conversation":
            self.calls += 1
            return LLMResult(text=None, model="scripted",
                             tool_calls=[{"name": "interpret_conversation", "arguments": dict(RELATIONSHIP_CLAIM)}])
        raise LLMUnavailable(f"scripted: no model for {name}")


async def say(eng, message):
    from engine.reasoning import ReasoningService
    from engine.shopper_v2.graph import run_turn_via_graph

    ad = eng.registry.adapter_for("conn_demo")
    cart_id = (await ad.create_cart()).cart_id
    client = Scripted()
    r = await run_turn_via_graph(
        reasoning=ReasoningService(client), adapter=ad, session_id=f"nm_{uuid.uuid4().hex[:8]}", connection_id="conn_demo",
        message=message, cart_id=cart_id, decision=eng.decision, gate=eng.gate, execution=eng.execution)
    return r, client, ad, cart_id


async def checks(eng):
    print("1. Two named products, model claims a relationship comparison")
    r, client, ad, cart_id = await say(eng, "Compare the Trailblazer Running Shoe and the Performance Socks Three Pack")
    got = [p.title for p in r.products]
    check("the model was actually consulted (the live path)", client.calls >= 1, client.calls)
    check("both named products are shown", set(got) == {"Trailblazer Running Shoe", "Performance Socks Three Pack"}, got)
    check("not the one-side question", "one side of that" not in r.reply, r.reply[:160])
    check("the cart is untouched", (await ad.get_cart(cart_id)).lines == [])

    print("2. Three named products")
    r, client, ad, cart_id = await say(
        eng, "Compare the Trailblazer Running Shoe, the Performance Socks Three Pack and the Energy Gel Box of 12")
    got = [p.title for p in r.products]
    check("all three named products are shown", set(got) == {
        "Trailblazer Running Shoe", "Performance Socks Three Pack", "Energy Gel Box of 12"}, got)

    print("3. One real product and one absent from the catalogue")
    r, client, ad, cart_id = await say(eng, "Compare the Trailblazer Running Shoe and the Quillfeather Zephyr")
    got = [p.title for p in r.products]
    check("the real one is shown, nothing invented", got == ["Trailblazer Running Shoe"], got)
    check("the missing one is named in the reply", "quillfeather" in r.reply.lower(), r.reply[:200])

    print("4. A comparison naming only categories never errors")
    r, client, ad, cart_id = await say(eng, "Compare running shoes and socks")
    check("no error text", "Traceback" not in r.reply and "Error" not in r.reply, r.reply[:160])


async def main():
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401 - registers the V2 tables before create_schema

    await create_schema()
    eng = build_engine()
    try:
        await checks(eng)
    finally:
        await eng.close()
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
