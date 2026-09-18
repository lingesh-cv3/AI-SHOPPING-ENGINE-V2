"""Two of CLAUDE.md's four real-client conditions, exercised directly
against `engine.shopper_v2.orchestrator.run_turn`: zero data (an empty
catalog) and platform down (the adapter raises). No live services, no LLM
key required - `ReasoningService.from_env()`-equivalent unavailable client
exercises the same rule-based fallback path V1 already uses when the model
is not configured, so this runs deterministically anywhere.

Uses its own isolated sqlite file - never cv3.db.

    python tests/shopper_v2/test_zero_data_platform_down.py
"""

import os
import sys

os.environ.setdefault(
    "DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_zerodata.db"
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402


class _EmptyAdapter:
    async def search_products(self, query, *, limit=20):
        return []


class _DownAdapter:
    async def search_products(self, query, *, limit=20):
        raise RuntimeError("platform is down")


async def main() -> None:
    from engine.db import create_schema, dispose
    from engine.reasoning.service import ReasoningService
    from engine.shopper_v2.orchestrator import run_turn

    await create_schema()
    reasoning = ReasoningService(None)  # no model configured -> rules fallback

    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
        if not ok:
            failures += 1

    # Zero data: a freshly-seeded merchant with nothing in the catalog.
    result = await run_turn(
        reasoning=reasoning,
        adapter=_EmptyAdapter(),
        session_id="sess_test_zero_data",
        connection_id="conn_demo",
        message="I need running shoes",
    )
    check("zero-data: no products fabricated", result.evidence.products == [])
    check(
        "zero-data: reply is honest, not a fabricated match",
        result.evidence.no_match_reason == "no_candidates",
    )
    check(
        "zero-data: reply text does not silently claim success",
        "couldn't find" in result.reply.lower(),
    )

    # Platform down: the adapter raises.
    result2 = await run_turn(
        reasoning=reasoning,
        adapter=_DownAdapter(),
        session_id="sess_test_platform_down",
        connection_id="conn_demo",
        message="I need running shoes",
    )
    check(
        "platform-down: no raw exception reached the caller",
        True,  # if we got here at all, run_turn did not propagate the raise
    )
    check(
        "platform-down: evidence honestly marks the platform unavailable",
        result2.evidence.no_match_reason == "platform_unavailable",
    )
    check(
        "platform-down: reply does not claim success",
        "reach the store" in result2.reply.lower(),
    )

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all zero-data / platform-down checks passed")
    print()

    await dispose()
    sys.exit(1 if failures else 0)


asyncio.run(main())
