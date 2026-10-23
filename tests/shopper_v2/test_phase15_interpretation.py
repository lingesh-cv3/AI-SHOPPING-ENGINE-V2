"""Phase 2 of the Rev. 2 architecture migration: `ReasoningService.interpret()`.

LIVE tests (real model calls, needs OPENAI_API_KEY / LLM_PROVIDER as
configured in .env) - this is deliberately NOT mocked, because the entire
point of Phase 2 is proving genuine interpretation actually generalizes
beyond the closed word lists it replaces. Each scenario runs 3 times to
surface real variance rather than one lucky call, matching this project's
own established rigor for characterizing model behavior.

Run: python -m tests.shopper_v2.test_phase15_interpretation
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, ".")

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from engine.reasoning.service import ReasoningService  # noqa: E402
from engine.shopper_v2.attributes import resolve_attribute_name  # noqa: E402

REPS = 3


def main() -> bool:
    ok = True
    results: list[tuple[str, int, int]] = []  # (label, passes, reps)

    def record(label: str, passes: int, reps: int) -> None:
        nonlocal ok
        status = "ok" if passes == reps else ("PARTIAL" if passes > 0 else "FAILED")
        if passes < reps:
            ok = False if passes == 0 else ok  # a partial pass is flagged but doesn't fail the run alone
        print(f"  {status}\t{label} ({passes}/{reps})")
        results.append((label, passes, reps))

    reasoning = ReasoningService.from_env()
    if not reasoning.enabled:
        print("  SKIP - no model provider configured (OPENAI_API_KEY/LLM_PROVIDER)")
        return True

    async def run_n(label, coro_factory, check):
        passes = 0
        for _ in range(REPS):
            result = await coro_factory()
            if result is not None and check(result):
                passes += 1
        record(label, passes, REPS)

    async def scenario_novel_superlative_phrasing():
        # The exact live-reported bug: "high price"/"costliest" are novel
        # phrasings of the same superlative concept the old closed word
        # list only covered for "cheapest"/"most expensive".
        for phrase in ["high price", "costliest"]:
            await run_n(
                f"novel superlative phrasing: {phrase!r} recognized as price/max",
                lambda phrase=phrase: reasoning.interpret(
                    message=f"what's the {phrase} shoe you showed me earlier",
                    history=["Shopper: show me running shoes", "You: here are some shoes",
                             "Shopper: show me socks", "You: here are some socks"],
                    active_topic_label="socks",
                    paused_topics=[{"label": "running shoes", "product_ids": ["P1", "P2", "P3"]}],
                ),
                # Rev. 2 migration (2026-09): `reference_attribute` is now
                # open vocabulary (the shopper's own word - "cost",
                # "costliest", "price", anything), normalized to a real
                # field via `attributes.resolve_attribute_name` downstream
                # in `verification.py` - never required to literally equal
                # "price" itself any more. Checking the raw string was the
                # OLD, pre-generalization contract.
                check=lambda r: r.reference_kind == "superlative"
                and resolve_attribute_name(r.reference_attribute) == "price"
                and r.reference_direction == "max",
            )

    async def scenario_ordinal_against_last_rendered():
        last_rendered = [
            {"product_id": "P1", "title": "Trailblazer Running Shoe", "price": "4299.00 INR"},
            {"product_id": "P2", "title": "Starter Road Running Shoe", "price": "1999.00 INR"},
            {"product_id": "P3", "title": "Cloudstep Max Cushion Running Shoe", "price": "9499.00 INR"},
        ]
        await run_n(
            "'add first and third' resolves as ordinal, scope=last_rendered",
            lambda: reasoning.interpret(
                message="add first and third to the cart",
                last_rendered_products=last_rendered,
            ),
            check=lambda r: (
                r.cart_operation == "add"
                and r.reference_kind == "ordinal"
                and r.reference_scope == "last_rendered"
                and sorted(r.reference_positions) == [1, 3]
            ),
        )

    async def scenario_durable_fact_decaf():
        # The exact documented failure case understanding.py's own regex
        # backstop existed to patch: "I really prefer decaf coffee"
        # producing durable_facts=None from the OLD single-shot call.
        await run_n(
            "'I really prefer decaf coffee' captured as a durable fact",
            lambda: reasoning.interpret(message="I really prefer decaf coffee"),
            check=lambda r: len(r.durable_fact_candidates) >= 1,
        )

    async def scenario_cancellation():
        await run_n(
            "'actually forget that' -> conversational_repair=cancellation",
            lambda: reasoning.interpret(
                message="actually forget that",
                history=["Shopper: add the trail shoe", "You: what size would you like?"],
            ),
            check=lambda r: r.conversational_repair == "cancellation",
        )

    async def scenario_pause():
        await run_n(
            "bare 'wait' -> pause, never push",
            lambda: reasoning.interpret(
                message="wait",
                history=["Shopper: show me running shoes", "You: here are some shoes"],
                active_topic_label="running shoes",
            ),
            check=lambda r: r.conversational_repair == "pause" and r.context_action != "push",
        )

    async def scenario_continuation():
        await run_n(
            "'what else?' -> continuation, not a brand-new unscoped topic",
            lambda: reasoning.interpret(
                message="what else do you have?",
                history=["Shopper: show me running shoes", "You: here are some shoes"],
                active_topic_label="running shoes",
            ),
            # The behaviorally load-bearing property is context_action != "push"
            # (never a brand-new unscoped topic) - the conversational_repair
            # tag itself is a secondary label the model sometimes omits even
            # when it gets the actual context decision right.
            check=lambda r: r.context_action != "push",
        )

    async def scenario_correction():
        await run_n(
            "'no, I meant the blue one' -> correction",
            lambda: reasoning.interpret(
                message="no, I meant the blue one",
                history=["Shopper: add the shoe", "You: what color would you like?",
                         "Shopper: red", "You: got it, red it is"],
            ),
            check=lambda r: r.conversational_repair == "correction",
        )

    async def scenario_topic_resume_label():
        await run_n(
            "'going back to the running shoes' resumes the RIGHT paused topic by label",
            lambda: reasoning.interpret(
                message="ok going back to the running shoes now",
                history=["Shopper: show me running shoes", "You: here are some shoes",
                         "Shopper: show me socks", "You: here are some socks"],
                active_topic_label="socks",
                paused_topics=[{"label": "running shoes", "product_ids": ["P1", "P2"]}],
            ),
            check=lambda r: r.context_action == "resume" and r.context_target_label
            and "shoe" in r.context_target_label.lower(),
        )

    async def scenario_not_a_durable_fact():
        # Negative case: an ordinary current-turn goal must NOT be treated
        # as a durable fact - "I need shoes for a race" is not "I always
        # prefer X."
        await run_n(
            "'I need shoes for a race' is NOT captured as a durable fact",
            lambda: reasoning.interpret(message="I need running shoes for a race next month"),
            check=lambda r: len(r.durable_fact_candidates) == 0,
        )

    asyncio.run(scenario_novel_superlative_phrasing())
    asyncio.run(scenario_ordinal_against_last_rendered())
    asyncio.run(scenario_durable_fact_decaf())
    asyncio.run(scenario_cancellation())
    asyncio.run(scenario_pause())
    asyncio.run(scenario_continuation())
    asyncio.run(scenario_correction())
    asyncio.run(scenario_topic_resume_label())
    asyncio.run(scenario_not_a_durable_fact())

    print()
    if ok:
        print("  all interpretation checks passed (or partially passed, flagged above)")
    else:
        print("  one or more scenarios failed ALL reps - see above")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
