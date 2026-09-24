"""Regression tests for Stage 2A Findings 2, 3 and 4's fixes, transcribed
from the live reproductions found during Phase 1 (see PROGRESS.md, Shopper
V2 section). Findings 2 and 3 are pure, deterministic function calls
against `engine.shopper_v2.evidence.build_evidence` - no network, no LLM.
Finding 4 needs the new `search_context` table, so it uses its own isolated
sqlite file, same convention as `test_zero_data_platform_down.py`.

    python tests/shopper_v2/test_phase1_findings.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase1.db")
# Deleted on every run, not just created once - this file's own state must
# never leak between runs (the same "deterministic/repeatable" standard
# Stage 2A's eval harness holds itself to), so a re-run never inherits a
# previous run's rows.
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)

os.environ.setdefault(
    "DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase1.db"
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

from shared.models import Availability, Money, Product  # noqa: E402

from engine.shopper_v2.evidence import build_evidence  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


def _p(product_id: str, title: str, description: str, price: str, rating: float) -> Product:
    amount, currency = price.split()
    return Product(
        product_id=product_id,
        title=title,
        description=description,
        categories=["Blends"],
        price=Money(amount=amount, currency=currency),
        availability=Availability.IN_STOCK,
        rating=rating,
        rating_count=50,
    )


def finding_2_five_candidates_no_priority_signal_keeps_the_real_match() -> None:
    """The exact `kb-lang-01` shape from SHOPPER_V2_BASELINE_REPORT.md:
    5 candidates, no priority/must-have/budget signal, two genuinely tied
    at the top lexical score - the lower-rated one must NOT be silently
    dropped purely by `mission_ranking.DEFAULT_LIMIT=4`."""
    candidates = [
        _p("A", "Morning Bell Espresso Blend", "Chocolate, toffee, a clean finish.", "1190.00 INR", 3.6),
        _p("B", "Espresso Subscription", "Always a blend that works in milk.", "1290.00 INR", 4.9),
        _p("C", "Nightshift Dark Roast", "Deep, smoky, unapologetic.", "990.00 INR", 4.8),
        _p("D", "House Filter Blend", "The everyday one.", "1050.00 INR", 4.7),
        _p("E", "Swiss Water Decaf", "Chemical-free decaffeination.", "1150.00 INR", 4.6),
    ]
    ev = build_evidence(
        query="espresso blends",
        raw_said="espresso blends",
        products=candidates,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=candidates,
        platform_unavailable=False,
    )
    verified_ids = {p.product_id for p in ev.products if p.relevance_verified}
    check(
        "Finding 2: both tied top-scoring candidates verified, not just the higher-rated one",
        verified_ids == {"A", "B"},
    )


def finding_3_single_hard_survivor_weak_match_is_confirmed() -> None:
    """A single-word query with exactly one real candidate must be
    confirmed, not withheld as 'weak' - nf-multi-01's 'sneakers' shape."""
    only_candidate = [
        _p("P1003", "Everyday Canvas Sneaker", "Simple canvas upper, rubber sole.", "1899.00 INR", 4.0),
    ]
    ev = build_evidence(
        query="sneakers",
        raw_said="I need sneakers",
        products=only_candidate,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=only_candidate,
        platform_unavailable=False,
    )
    check(
        "Finding 3: sole hard-filter survivor with a weak single-word match is confirmed",
        ev.products[0].relevance_verified is True and ev.products[0].weak_match_overridden is True,
    )


def finding_3_does_not_override_a_diverging_query() -> None:
    """The override must never apply when the query diverges from what the
    shopper actually said - that is a different, unrelated risk (a model-
    invented proxy term), and Finding 3's fix must not weaken it."""
    only_candidate = [
        _p("P3001", "Insulated Water Bottle 750ml", "Keeps cold for 24 hours.", "899.00 INR", 4.0),
    ]
    ev = build_evidence(
        query="water",
        raw_said="I need a kayak",
        products=only_candidate,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=only_candidate,
        platform_unavailable=False,
    )
    check(
        "Finding 3 override does not apply to a diverging query (kayak/water-bottle stays unverified)",
        ev.products[0].relevance_verified is False,
    )


def finding_3_generalizes_to_several_genuine_tied_survivors() -> None:
    """Superseded assertion, corrected live (2026-09-20): a category browse
    with several real, equally-matching products used to be treated the
    same as a genuinely ambiguous single rival, and withheld ALL of them -
    this was the exact "show me shoes only shows 1 product" bug a real
    shopper hit. Two real shoes both scoring top on "shoe", with no
    lower-scoring outlier riding along, are not a rival pair to be
    withheld from each other - they are the whole answer, and both must be
    verified. See `evidence.py`'s `all_survivors_are_top_matches` (the
    generalization of the old `single_hard_survivor` rule this test used
    to name)."""
    candidates = [
        _p("P1", "Marathon Pro Racing Shoe", "Carbon plate.", "12499.00 INR", 4.0),
        _p("P2", "Court Classic Tennis Shoe", "Flat sole.", "3499.00 INR", 4.0),
    ]
    ev = build_evidence(
        query="shoe",
        raw_said="a shoe",
        products=candidates,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=candidates,
        platform_unavailable=False,
    )
    check(
        "Finding 3 override now applies to every tied genuine survivor, not just a lone one",
        all(p.relevance_verified for p in ev.products),
    )


def finding_3_ordinary_multiword_match_still_discriminates() -> None:
    """Corrected 2026-09-24 - this test used to assert that a real,
    genuinely-scoring-lower rival blocks Finding 3's override. That
    premise is unreachable: `weak_single_word_match` (the ONLY gate the
    override can ever affect) is true exclusively when `top_score == 1`,
    and no candidate can score strictly between 0 and 1 (overlap counts
    are non-negative integers) - so the "real, nonzero, lower-scoring
    rival" this test's old fixture claimed to construct never actually
    existed; the fixture's second candidate ("Trail Running Cap" for
    query "shoe") in fact scored exactly 0, which is
    `finding_3_zero_overlap_survivor_never_blocks_a_real_match`'s case,
    not this one - and asserting it should stay withheld was itself the
    bug the live "something for energy" report exposed. What remains
    genuinely worth testing here: an ordinary multi-word match
    (`top_score >= 2`, never weak in the first place) still correctly
    verifies the real match and withholds an unrelated candidate - the
    override mechanism is irrelevant to this case, by construction."""
    candidates = [
        _p(
            "P1", "Packable Rain Jacket",
            "Lightweight, waterproof, packs into its own pocket.", "4999.00 INR", 4.0,
        ),
        _p(
            "P2", "Lightweight Base Layer Top",
            "Moisture-wicking layer for cold starts.", "1999.00 INR", 4.0,
        ),
    ]
    ev = build_evidence(
        query="lightweight waterproof",
        raw_said="something lightweight and waterproof",
        products=candidates,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=candidates,
        platform_unavailable=False,
    )
    by_id = {p.product_id: p for p in ev.products}
    check(
        "Finding 3: a genuine two-word match verifies; a one-word-weaker candidate does not",
        by_id["P1"].relevance_verified and not by_id["P2"].relevance_verified,
    )


def finding_3_zero_overlap_survivor_never_blocks_a_real_match() -> None:
    """Live-reproduced, platform-agnostic gap (2026-09-24): a mission item
    labeled "something for energy" retrieved a real energy product (one
    genuine word overlap on "energy") ALONGSIDE an unrelated sock that
    shares ZERO query words - the sock only "survived" because nothing
    about it violates budget/stock/exclusions, not because it is a
    plausible second reading of "energy". A hard-filter survivor with NO
    real word overlap at all is not a rival interpretation and must never
    block the single-strong-match override - see
    `engine.execution.relevance.any_overlap_ids`'s own docstring. Same
    shape, any category: a "shoe" query alongside a completely unrelated
    "cap" survivor must verify the shoe, not withhold both."""
    candidates = [
        _p("P1", "Marathon Pro Racing Shoe", "Carbon plate build for shoe fans.", "12499.00 INR", 4.0),
        _p(
            "P2", "Trail Running Cap",
            "A breathable cap for trail runners.", "1299.00 INR", 4.0,
        ),
    ]
    ev = build_evidence(
        query="shoe",
        raw_said="a shoe",
        products=candidates,
        shopping_state=None,
        memory_facts=None,
        catalog_sample=candidates,
        platform_unavailable=False,
    )
    by_id = {p.product_id: p for p in ev.products}
    check(
        "Finding 3: a zero-overlap survivor never blocks a real single-word match",
        by_id["P1"].relevance_verified and not by_id["P2"].relevance_verified,
    )


async def finding_4_search_context_round_trip() -> None:
    from engine.db import create_schema, dispose
    from engine.shopper_v2 import search_context

    await create_schema()
    none_yet = await search_context.get_last_effective_query("sess_f4", "conn_demo")
    check("Finding 4: no stored query before any is set", none_yet is None)

    await search_context.set_last_effective_query("sess_f4", "conn_demo", "running shoes")
    stored = await search_context.get_last_effective_query("sess_f4", "conn_demo")
    check("Finding 4: stored query round-trips", stored == "running shoes")

    await search_context.set_last_effective_query("sess_f4", "conn_demo", "  ")
    stored_after_blank = await search_context.get_last_effective_query("sess_f4", "conn_demo")
    check(
        "Finding 4: a blank query never overwrites a real stored one",
        stored_after_blank == "running shoes",
    )

    other_conn = await search_context.get_last_effective_query("sess_f4", "conn_kettle")
    check(
        "Finding 4: scoped per (session_id, connection_id) - a different connection sees nothing",
        other_conn is None,
    )

    await dispose()


def main() -> int:
    finding_2_five_candidates_no_priority_signal_keeps_the_real_match()
    finding_3_single_hard_survivor_weak_match_is_confirmed()
    finding_3_does_not_override_a_diverging_query()
    finding_3_generalizes_to_several_genuine_tied_survivors()
    finding_3_ordinary_multiword_match_still_discriminates()
    finding_3_zero_overlap_survivor_never_blocks_a_real_match()
    asyncio.run(finding_4_search_context_round_trip())

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 1 finding-fix checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
