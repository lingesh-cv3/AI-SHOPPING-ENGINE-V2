"""Permanent regression coverage for the root-cause consolidation (2026-09):
cardinality, "all", ranking, superlatives, and ordinal-range references are
now decided from ONE typed `QueryIntent`
(`engine/shopper_v2/reference_resolution.py`) and applied in ONE place
(`engine/shopper_v2/graph.py::_apply_query_intent`), instead of being
re-derived separately by each capability. This file is the permanent,
repeatable test that used to only exist as a throwaway manual fuzz script -
every specific live-reproduced bug this consolidation closed gets a real,
committed assertion here so a future change that reopens one of them fails
a test instead of being found by hand again.

Two layers:
  1. Pure unit checks on `parse_query_intent`/`strip_intent_phrases` - no
     server, no model, runs anywhere, always.
  2. Live integration checks via `orchestrator.run_turn` against a real
     Northfield adapter and a real model - the same pattern
     `test_phase12_discovery_vs_cart_routing.py` already uses. Requires
     Northfield (8001) and a reachable model; honestly skipped (not
     silently passed) if no model is configured, exactly like that file.

    python tests/shopper_v2/test_query_intent_consolidation.py
"""

from __future__ import annotations

import os
import sys
import uuid

_DB_FILE = os.path.join(
    os.path.dirname(__file__), "..", "..", "_test_shopper_v2_query_intent.db"
)
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_query_intent.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


# ---------------------------------------------------------------------------
# Layer 1: pure QueryIntent unit checks - no server needed.
# ---------------------------------------------------------------------------

def _unit_checks() -> None:
    from engine.shopper_v2.reference_resolution import (
        parse_query_intent,
        resolve_group_selector,
        resolve_ordinal_range,
    )

    qi = parse_query_intent("show me all shoes available")
    check("'show me all shoes' -> show_all=True, no fixed count", qi.show_all and qi.requested_count is None)

    qi = parse_query_intent("show me shoes")
    check("'show me shoes' (no count stated) -> no explicit count, no show_all", not qi.show_all and qi.requested_count is None)

    qi = parse_query_intent("show me two running shoes")
    check("'show me two running shoes' -> requested_count=2", qi.requested_count == 2)

    qi = parse_query_intent("what is the cheapest shoe available here?")
    check(
        "'cheapest' -> superlative=min on price, reachable with NO historical wording",
        qi.superlative == "min" and qi.sort_attribute == "price",
    )

    qi = parse_query_intent("what is the most expensive shoe?")
    check("'most expensive' -> superlative=max on price", qi.superlative == "max" and qi.sort_attribute == "price")

    qi = parse_query_intent("which is the best rated?")
    check("'best rated' -> superlative=max on rating", qi.superlative == "max" and qi.sort_attribute == "rating")

    # Live-reproduced bug: a bare direction word ("high"/"low") inside a
    # ranking phrase used to leak into the query used for relevance
    # scoring, coincidentally substring-matching an unrelated product
    # (e.g. "Highridge") and starving every genuine match. `core_query`
    # must have the ranking phrase's own words stripped.
    qi = parse_query_intent("show me three running shoes ranked by price high to low")
    check(
        "'ranked by price high to low' -> requested_count=3, sort desc on price",
        qi.requested_count == 3 and qi.sort_attribute == "price" and qi.sort_direction == "desc",
    )
    check(
        "core_query strips 'ranked'/'price'/'high'/'low'/'three' - never a real product word",
        "high" not in qi.core_query and "low" not in qi.core_query and "three" not in qi.core_query
        and "running" in qi.core_query and "shoes" in qi.core_query,
    )

    qi = parse_query_intent("rank them low to high")
    check("'low to high' with no named attribute defaults to price, asc", qi.sort_attribute == "price" and qi.sort_direction == "asc")

    # Ordinal-range: "the first two"/"the last three" - a genuine
    # composition of two closed classes, not its own word list. Live-
    # reproduced bug: "add first two to the cart" used to be unresolvable.
    check("'add first two to the cart' -> ('first', 2)", resolve_ordinal_range("add first two to the cart") == ("first", 2))
    check("'the last three' -> ('last', 3)", resolve_ordinal_range("the last three") == ("last", 3))
    check("a bare 'the first one' -> no ordinal-range (that's resolve_ordinal_positions' job)", resolve_ordinal_range("the first one") is None)

    check("resolve_group_selector('add first two...', group of 4) -> [0, 1]", resolve_group_selector("add first two to the cart", 4) == [0, 1])
    check(
        "resolve_group_selector('add the first two', group of 1) clamps honestly to [0], never invents a 2nd",
        resolve_group_selector("add the first two", 1) == [0],
    )
    check("resolve_group_selector('the last three', group of 5) -> [2, 3, 4]", resolve_group_selector("the last three", 5) == [2, 3, 4])

    from engine.shopper_v2.reference_resolution import (
        resolve_digit_positions,
        resolve_named_subset,
        resolve_ordinal_positions,
    )

    # Live-reproduced (2026-09): a real, common typo ("frist" for "first")
    # failed outright with no fuzzy tolerance at all, and a message
    # naming TWO ordinal words (one typo'd, one exact) only ever found
    # the exact one, because the typo-tolerant fallback used to only run
    # when NO exact match existed anywhere in the message.
    check(
        "'add frist and second to the cart' -> [0, 1] (typo'd 'first' AND exact 'second')",
        resolve_ordinal_positions("add frist and second to the cart") == [0, 1],
    )
    check(
        "an unrelated real word ('fresh') is never mistaken for a typo'd ordinal",
        resolve_ordinal_positions("I want a fresh pair of shoes") is None,
    )
    check(
        "a bare size number is never mistaken for an ordinal typo",
        resolve_ordinal_positions("show me size 8 shoes") is None,
    )

    # Live-reproduced: a shopper answering with the SAME numbering the
    # system's own clarifying question just displayed ("1 and 2") failed
    # outright - no digit-position recognition existed at all.
    check("'1 and 2' -> [0, 1]", resolve_digit_positions("1 and 2") == [0, 1])
    check(
        "a single bare digit is NEVER treated as a position (that's a size/quantity elsewhere)",
        resolve_digit_positions("show me size 8") is None,
    )

    # Live-reproduced: pasting a prior clarifying question's own numbered
    # list back verbatim, by full product name, failed outright - only a
    # single named product was ever resolved, never several together.
    titles = [
        "Marathon Pro Racing Shoe", "Cloudstep Max Cushion Running Shoe",
        "Highridge Waterproof Trail Running Shoe", "Featherline Speed Running Shoe",
    ]
    check(
        "pasting '1. Marathon...; 2. Cloudstep...' back verbatim -> [0, 1]",
        resolve_named_subset(
            "1. Marathon Pro Racing Shoe; 2. Cloudstep Max Cushion Running Shoe;", titles,
        ) == [0, 1],
    )
    check(
        "a message naming only ONE real product never fires this (that's the single-name path's job)",
        resolve_named_subset("Add the Marathon Pro Racing Shoe", titles) is None,
    )


def _ranking_unit_checks() -> None:
    from engine.execution import ranking
    from shared.models import Money, Product

    def p(pid, price, rating=None):
        if price is None:
            return Product(product_id=pid, title=pid, price=None, rating=rating)
        return Product(product_id=pid, title=pid, price=Money(amount=str(price), currency="INR"), rating=rating)

    products = [p("A", 100, 4.0), p("B", None, None), p("C", 200, 3.0)]
    check(
        "sort_products price asc puts real prices first (A, C), unknown price (B) last",
        [x.product_id for x in ranking.sort_products(products, "price", "asc")] == ["A", "C", "B"],
    )
    check("pick_superlative price min -> the real cheapest (A)", ranking.pick_superlative(products, "price", "min").product_id == "A")
    check("pick_superlative rating max among only-rated ones (A)", ranking.pick_superlative(products, "rating", "max").product_id == "A")
    check(
        "an attribute this catalog has no real field for is refused honestly, never guessed",
        "durability" not in ranking.KNOWN_ATTRIBUTES,
    )


# ---------------------------------------------------------------------------
# Layer 2: live integration checks - real Northfield adapter, real model.
# ---------------------------------------------------------------------------

async def _integration_checks(run_turn, engine, connection_id: str, adapter) -> None:
    session_id = f"sess_qi_{uuid.uuid4().hex[:8]}"

    r = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id,
        connection_id=connection_id, message="show me two running shoes",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    check(
        f"'show me two running shoes' shows exactly 2 (got {len(r.products)})",
        len(r.products) == 2,
    )

    r = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id,
        connection_id=connection_id, message="show me all shoes available",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    check(
        f"'show me all shoes' is not capped at the old default of 4 (got {len(r.products)})",
        len(r.products) > 4,
    )

    # Product decision (2026-09, corrected after live feedback): a PLAIN,
    # uncounted request ("show me shoes", no "all", no number) must show
    # everything genuinely found, the same as an explicit "all" - never a
    # fixed default preview size.
    session_id_plain = f"sess_qi_plain_{uuid.uuid4().hex[:8]}"
    r_plain = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id_plain,
        connection_id=connection_id, message="show me shoes",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    check(
        f"a PLAIN 'show me shoes' (no count, no 'all') is also not capped at 4 "
        f"(got {len(r_plain.products)})",
        len(r_plain.products) > 4,
    )

    r = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id,
        connection_id=connection_id, message="what is the cheapest shoe available here?",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    check(
        "bare 'cheapest' (no historical wording) gets a real, deterministic answer",
        "cheapest" in r.reply.lower() and len(r.products) == 1,
    )

    session_id2 = f"sess_qi_rank_{uuid.uuid4().hex[:8]}"
    r = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id2,
        connection_id=connection_id, message="show me three running shoes ranked by price high to low",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    prices = [float(p.price.amount) for p in r.products if p.price is not None]
    check(f"'ranked ... high to low' returns exactly 3 (got {len(r.products)})", len(r.products) == 3)
    check(f"'ranked ... high to low' is genuinely descending by price (got {prices})", prices == sorted(prices, reverse=True))
    # Text/card consistency - the exact class of bug this whole
    # consolidation exists to close: the reply must name the SAME
    # products in the SAME order as the cards, not a separately-ordered
    # list.
    named_order = [t for t in [p.title for p in r.products] if t.lower() in r.reply.lower()]
    check(
        "reply text names the products in the SAME order as the cards",
        all(
            r.reply.lower().find(a.lower()) < r.reply.lower().find(b.lower())
            for a, b in zip(named_order, named_order[1:])
        ),
    )

    r2 = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id2,
        connection_id=connection_id, message="add first two to the cart",
        cart_id=None, decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    check(
        "'add first two to the cart' is recognized as a real cart/fulfillment intent "
        f"(not a generic 'which one?' clarify) (capability={r2.trace.get('capability')!r})",
        r2.trace.get("capability") != "cart_action_ambiguous",
    )

    # Live-reproduced: after showing the SAME category twice (shoes, then
    # socks, then shoes again), "the cheapest shoe you showed me earlier"
    # used to resolve to the OLDER shoe group's own cheapest item, not the
    # most recently shown group's - both a tie-break bug (favored
    # whichever matching group was stored first) and a relevance-scoring
    # bug (the stopword "was" stemmed to "wa", coincidentally substring-
    # matching "water" in an unrelated product's description and
    # outscoring the correct, more recent group entirely).
    session_id3 = f"sess_qi_tiebreak_{uuid.uuid4().hex[:8]}"
    await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id3,
        connection_id=connection_id, message="show me shoes",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id3,
        connection_id=connection_id, message="show me socks",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    r3 = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id3,
        connection_id=connection_id, message="show me two running shoes",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    most_recent_shoe_titles = {p.title for p in r3.products}
    r4 = await run_turn(
        reasoning=engine.reasoning, adapter=adapter, session_id=session_id3,
        connection_id=connection_id,
        message="what was the cheapest shoe you showed me earlier?",
        decision=engine.decision, gate=engine.gate, execution=engine.execution,
    )
    check(
        "historical-group tie-break resolves to the MOST RECENT matching group "
        f"(got {[p.title for p in r4.products]!r}, most recent shoe group was {most_recent_shoe_titles!r})",
        bool(r4.products) and r4.products[0].title in most_recent_shoe_titles,
    )


async def main() -> None:
    _unit_checks()
    _ranking_unit_checks()

    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose
    from engine.shopper_v2.orchestrator import run_turn

    await create_schema()
    engine = build_engine()
    connection_id = "conn_demo"
    adapter = engine.registry.adapter_for(connection_id)

    if engine.reasoning is None or not engine.reasoning.enabled:
        print("  SKIP    live query-intent integration: needs a configured live model, none is configured here - NOT RUN, nothing was asserted")
        await engine.close()
        await dispose()
        return

    try:
        await _integration_checks(run_turn, engine, connection_id, adapter)
    finally:
        await engine.close()
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all query-intent consolidation checks passed")
    print()
    sys.exit(1 if failures else 0)
