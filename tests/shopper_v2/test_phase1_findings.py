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


def finding_3_does_not_override_with_a_real_rival_present() -> None:
    """The override only applies when there is exactly one hard-filter
    survivor - a genuine ambiguous one-word match with a real rival must
    still be withheld."""
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
        "Finding 3 override does not apply with more than one hard-filter survivor",
        not any(p.relevance_verified for p in ev.products),
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
    finding_3_does_not_override_with_a_real_rival_present()
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
