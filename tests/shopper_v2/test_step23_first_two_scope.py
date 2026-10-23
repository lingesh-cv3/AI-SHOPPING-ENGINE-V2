"""A collective ordinal comparison ("compare the first two") is scoped to the CURRENT view, whatever the model claims.

Live-reproduced in the Step 22 provider-backed run (gpt-4o-mini; 2 of 4 runs): after "show me running
shoes", "show me socks", "go back to the running shoes", the message "compare the first two" came back
from the model as a two-sided relationship whose second side pointed at the paused socks topic. The
shopper was told "Performance Socks Three Pack is cheaper than the Running Shoe Trail" - a shoe compared
with socks. Root cause: "the first two" (an ordinal range) was not recognised by the deterministic
collective-comparison guard (only "both"/"these"/"the two"/...), so the model's unconfirmed two-sided claim
decided the turn, and the compare node accepted the claim's resolved ids without checking they lie in the
current view.

The provider is a scripted double (the shape of test_phase7_acceptance_matrix.py): it returns the kinds of
claims the live model produced. Everything else is the real pipeline over the real sample merchants.

    python tests/shopper_v2/test_step23_first_two_scope.py        (needs Northfield on 8001, Kettle on 8002)
"""
from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_step23_first_two.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_step23_first_two.db"
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
        self.sid = f"s23_{uuid.uuid4().hex[:10]}"
        self.client = ScriptedClient()
        self.reasoning = ReasoningService(self.client)
        self.adapter = engine.registry.adapter_for(connection_id)

    async def say(self, message, model_claim):
        from engine.shopper_v2.graph import run_turn_via_graph

        self.client.queue.append(model_claim)
        return await run_turn_via_graph(
            reasoning=self.reasoning, adapter=self.adapter, session_id=self.sid, connection_id=self.cid,
            message=message, cart_id=None, decision=self.engine.decision, gate=self.engine.gate,
            execution=self.engine.execution)


def titles(r):
    return [p.title for p in r.products]


SOCKS = "Performance Socks Three Pack"

# Claims of the kinds the live model produced for "compare the first two" after the topic switch.
BAD_CLAIMS = {
    "two-sided: shoe ordinal + paused socks topic": claim(
        capability="compare", question_type="compound_comparison", reference_scope="last_rendered",
        reference_kind="ordinal", reference_positions=[2],
        secondary_reference={"reference_scope": "paused_topic", "reference_kind": "all"}),
    "two-sided: shoe ordinal + socks by name": claim(
        capability="compare", question_type="relationship", reference_scope="last_rendered",
        reference_kind="ordinal", reference_positions=[1],
        secondary_reference={"reference_scope": "named", "reference_kind": "named", "reference_names": [SOCKS]}),
    "relationship, socks by name only": claim(
        question_type="relationship", reference_scope="named", reference_kind="named", reference_names=[SOCKS]),
    "plain discovery": claim(),
    "paused-topic reference": claim(capability="compare", reference_scope="paused_topic", reference_kind="all"),
    "no interpretation (provider failed)": None,
}


async def after_topic_switch(eng):
    s = Session(eng, "conn_demo")
    r1 = await s.say("show me running shoes", claim(context_action="push"))
    await s.say("show me socks", claim(context_action="push"))
    r3 = await s.say("go back to the running shoes", claim())
    return s, titles(r1), titles(r3)


async def scenarios(eng):
    print("1. After a topic switch back to running shoes, 'compare the first two' is the first two SHOES")
    for label, bad in BAD_CLAIMS.items():
        s, shoes, back = await after_topic_switch(eng)
        if label == "plain discovery":
            check("setup: returning to running shoes shows the shoes again", len(back) >= 2 and SOCKS not in back)
        r = await s.say("compare the first two", bad)
        check(f"[{label}] exactly the first two of the shoes in view",
              sorted(titles(r)) == sorted(back[:2]) and SOCKS not in r.reply, (titles(r), r.reply[:150]))

    print("\n2. A correct model claim still works")
    s, shoes, back = await after_topic_switch(eng)
    r = await s.say("compare the first two", claim(
        capability="compare", question_type="retrieve", reference_scope="last_rendered",
        reference_kind="ordinal", reference_positions=[1, 2]))
    check("ordinal [1,2] over the view -> the first two shoes", sorted(titles(r)) == sorted(back[:2]))

    print("\n3. Other group selectors over the current view")
    for phrase, want in (("compare the first three", 3), ("compare the last two", -2)):
        s, shoes, back = await after_topic_switch(eng)
        r = await s.say(phrase, claim(question_type="relationship", reference_scope="named", reference_kind="named",
                                       reference_names=[SOCKS]))
        expected = back[:3] if want == 3 else back[-2:]
        check(f"{phrase!r}: exactly that slice of the shoes, never socks",
              sorted(titles(r)) == sorted(expected) and SOCKS not in titles(r), (titles(r), r.reply[:120]))
    s, shoes, back = await after_topic_switch(eng)
    r = await s.say("compare 1 and 3", claim(question_type="relationship", reference_scope="named",
                                              reference_kind="named", reference_names=[SOCKS]))
    check("'compare 1 and 3': positions 1 and 3 of the shoes in view",
          sorted(titles(r)) == sorted([back[0], back[2]]), (titles(r), r.reply[:120]))

    print("\n4. An explicitly named second side is still honoured (cross-topic comparison the shopper asked for)")
    s, shoes, back = await after_topic_switch(eng)
    r = await s.say("compare the Trailblazer Running Shoe with the Performance Socks Three Pack", claim(
        capability="compare", question_type="relationship", reference_scope="named", reference_kind="named",
        reference_names=["Trailblazer Running Shoe"],
        secondary_reference={"reference_scope": "named", "reference_kind": "named", "reference_names": [SOCKS]}))
    # Pre-existing limitation (unchanged by Step 23; the message names a second side so the collective guard
    # is inert): a named second side in a PAUSED topic is not always resolved - the engine then asks, honestly.
    # The safety property asserted here: the correct pair, or an honest question; never a wrong pair.
    check("a comparison that NAMES both sides across topics: the right pair, or an honest question - never a wrong pair",
          ("Trailblazer" in r.reply and "Socks" in r.reply)
          or ("which other product" in r.reply.lower() and not titles(r)), (titles(r), r.reply[:200]))

    print("\n5. Genuinely ambiguous / unresolvable: never invent a pair from another topic")
    s = Session(eng, "conn_demo")
    await s.say("show me running shoes", claim(context_action="push"))
    r2 = await s.say("show me socks", claim(context_action="push"))
    check("setup: only one product (the socks) is in view", titles(r2) == [SOCKS])
    r = await s.say("compare the first two", claim(question_type="relationship", reference_scope="last_rendered",
                                                    reference_kind="ordinal", reference_positions=[1, 2]))
    check("with ONE product in view it does not fabricate a two-product comparison with the earlier shoes",
          len(r.products) < 2 and not any("Running" in t for t in titles(r)))

    print("\n6. Tenant/merchant scope: the candidate set only ever holds the active merchant's products")
    s = Session(eng, "conn_kettle")
    await s.say("show me coffee", claim(context_action="push"))
    r = await s.say("compare the first two", claim(question_type="relationship", reference_scope="named",
                                                    reference_kind="named", reference_names=[SOCKS]))
    check("on Kettle the pair is two Kettle coffees; no Northfield product can appear",
          len(r.products) == 2 and SOCKS not in titles(r) and not any("Running" in t for t in titles(r)))

    print("\n7. Detector units")
    from engine.shopper_v2.capability import looks_like_collective_comparison as f
    for m, want in (("compare the first two", True), ("compare the first three", True), ("Compare the last two", True),
                    ("compare 1 and 3", True), ("compare both", True), ("compare these", True),
                    ("compare the first two with the socks", False), ("compare the first shoe and the socks", False),
                    ("compare the first shoe versus the Trailblazer", False), ("show me the first two", False),
                    ("add the first two", False), ("compare the first one", False), ("compare", False)):
        check(f"looks_like_collective_comparison({m!r}) is {want}", f(m) is want)


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
