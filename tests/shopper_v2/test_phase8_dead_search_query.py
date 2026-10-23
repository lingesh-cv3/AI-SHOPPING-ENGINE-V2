"""Dead-search hand-off carries the searched query as STRUCTURED data.

Live root cause: the storefront's dead-search hand-off sent only a synthesized
sentence (`I searched for "coffee" and nothing came up.`) plus
`friction=DEAD_SEARCH`. Friction recovery never read the sentence, so it could
only list departments - on Kettle & Bloom, typing "coffee" into the shop search
bar dead-ended even though the same word typed in chat returns the catalogue.
`ChatRequestV2.friction_query` now carries the term; the engine runs ordinary
discovery on it (vocabulary normalization + category-grounded browse included)
and falls back to the department list only when nothing is found.

No model, no provider key: the model double always fails, so every result
below comes from the deterministic path. Requires Northfield (8001) and Kettle
(8002). Own isolated sqlite file.

    python tests/shopper_v2/test_phase8_dead_search_query.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase8_ds.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_shopper_v2_phase8_ds.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
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
    """Answers the interpretation call with ONE scripted claim (the model's
    reading of the shopper's term); every other model call is unavailable."""

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


async def turn(engine, cid: str, message: str, *, friction=None, friction_query=None, cart_id=None, client=None):
    from engine.reasoning import ReasoningService
    from engine.shopper_v2.graph import run_turn_via_graph

    import inspect

    kwargs = {}
    # Old code has no such parameter: the query is then simply lost (the bug),
    # which is what the assertions below must observe rather than a TypeError.
    if friction_query is not None and "friction_query" in inspect.signature(run_turn_via_graph).parameters:
        kwargs["friction_query"] = friction_query
    return await run_turn_via_graph(
        reasoning=ReasoningService(client or DownClient()),
        adapter=engine.registry.adapter_for(cid),
        session_id=f"ds_{uuid.uuid4().hex[:10]}",
        connection_id=cid,
        message=message,
        friction=friction,
        cart_id=cart_id,
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
        **kwargs,
    )


def sentence(q: str) -> str:
    # exactly what storefront/src/App.tsx sends as `message`
    return f'I searched for "{q}" and nothing came up.'


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    import engine.shopper_v2.graph  # noqa: F401  (registers its tables)

    await create_schema()
    engine = build_engine()
    try:
        # -- the reported failure: the structured query drives recovery ------
        for q in ("coffee", "cofffee", "coffee blends"):
            r = await turn(engine, "conn_kettle", sentence(q), friction="DEAD_SEARCH", friction_query=q)
            check(f"Kettle dead search {q!r} with its query returns real products", len(r.products) > 0)
            check(f"  {q!r}: used no model", r.used_model is False)

        # -- before/after control: without the query, behaviour is unchanged --
        r = await turn(engine, "conn_kettle", sentence("coffee"), friction="DEAD_SEARCH")
        check("no friction_query: the department-list recovery reply is preserved", "This store carries" in r.reply and not r.products)
        check("no friction_query: routed to friction recovery", r.trace.get("capability") == "friction_recovery")

        # -- a query that genuinely matches nothing still ends honestly -------
        r = await turn(engine, "conn_kettle", sentence("zzqxjv"), friction="DEAD_SEARCH", friction_query="zzqxjv")
        check("a genuinely dead query returns no products", len(r.products) == 0)
        check("a genuinely dead query still gets the department list, not a bare apology", "This store carries" in r.reply)
        check("  ...and no degradation noise is appended to the recovery reply", "fully process" not in r.reply)

        # -- with a model: the term reaches the model-driven category grounding --
        # "beans" is a product-type word no title/category contains; only the
        # model's reading (a catalog category, validated against the store's
        # real taxonomy) can resolve it. The structured query must reach that
        # path exactly as a typed message does.
        r = await turn(engine, "conn_kettle", sentence("beans"), friction="DEAD_SEARCH", friction_query="beans",
                       client=ScriptedClient(claim(catalog_categories=["Single origin", "Blends"])))
        check("scripted model reads 'beans' as real categories: the dead search returns beans", len(r.products) >= 5)
        check("  ...every product returned is a real Kettle bean/blend title",
              all(p.title in {"Ethiopia Guji Natural", "Colombia Huila Washed", "Brazil Cerrado Pulped Natural",
                              "Morning Bell Espresso Blend", "Nightshift Dark Roast", "House Filter Blend",
                              "Swiss Water Decaf"} for p in r.products))
        # Step 24: "beans" is now also grounded from variant options (a real, catalogue-evidenced path), so the
        # invented-category probe uses a word with no evidence anywhere; the property asserted is unchanged.
        r = await turn(engine, "conn_kettle", sentence("flibbertigibbet"), friction="DEAD_SEARCH",
                       friction_query="flibbertigibbet",
                       client=ScriptedClient(claim(catalog_categories=["Nonexistent Category"])))
        check("a model-invented category is rejected by validation (no fabricated products)", len(r.products) == 0)

        # -- Northfield: same mechanism, other platform, nothing merchant-specific
        r = await turn(engine, "conn_demo", sentence("socks"), friction="DEAD_SEARCH", friction_query="socks")
        check("Northfield dead search with its query returns real products", len(r.products) > 0)

        # -- search-bar text is data, never a command ------------------------
        cart = await engine.registry.adapter_for("conn_demo").create_cart()
        r = await turn(engine, "conn_demo", sentence("add the trailblazer to my cart"),
                       friction="DEAD_SEARCH", friction_query="add the trailblazer to my cart", cart_id=cart.cart_id)
        check("a command-shaped search term is routed as discovery, never a cart action", r.trace.get("capability") == "discover")
        after = await engine.registry.adapter_for("conn_demo").get_cart(cart.cart_id)
        check("  ...and the cart is untouched", len(getattr(after, "lines", []) or []) == 0)

        # -- the query only means anything for a dead search -----------------
        r = await turn(engine, "conn_kettle", "my promo code did not work", friction="PROMOTION_FAILED", friction_query="coffee")
        check("friction_query is ignored for other friction types", r.trace.get("capability") == "friction_recovery" and not r.products)
        r = await turn(engine, "conn_kettle", "hello", friction=None, friction_query="coffee")
        check("friction_query is ignored when there is no friction signal", r.trace.get("capability") != "friction_recovery" and not any("Guji" in p.title for p in r.products))

        # -- whitespace-only query is treated as absent ----------------------
        r = await turn(engine, "conn_kettle", sentence("coffee"), friction="DEAD_SEARCH", friction_query="   ")
        check("a blank friction_query falls back to the old recovery behaviour", r.trace.get("capability") == "friction_recovery")
    finally:
        await engine.close()
        await dispose()

    # -- request schema ---------------------------------------------------------
    from pydantic import ValidationError

    from engine.api.chat_v2 import ChatRequestV2

    check("ChatRequestV2 declares friction_query", "friction_query" in ChatRequestV2.model_fields)
    base = dict(connection_id="conn_kettle", session_id="s", message="m")
    check("friction_query defaults to None", getattr(ChatRequestV2(**base), "friction_query", "MISSING") is None)
    try:
        ChatRequestV2(**base, friction_query="x" * 500)
        rejected = False
    except ValidationError:
        rejected = True
    check("an over-long friction_query is rejected at the schema", rejected)


asyncio.run(main())
print()
print(f"  {failures} FAILURE(S)" if failures else "  all dead-search-query checks passed")
sys.exit(1 if failures else 0)
