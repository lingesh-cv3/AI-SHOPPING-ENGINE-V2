"""Phase 1 capability routing (`engine/shopper_v2/capability.py`).

Model-free, pure function - no live service required.

    python tests/shopper_v2/test_phase1_capability_routing.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from engine.shopper_v2.capability import Capability, route  # noqa: E402


def main() -> int:
    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
        if not ok:
            failures += 1

    check("None hint routes to DISCOVER", route(None) is Capability.DISCOVER)
    check(
        "RECOMMEND_PRODUCTS routes to DISCOVER",
        route("RECOMMEND_PRODUCTS") is Capability.DISCOVER,
    )
    check(
        "COMPARE_PRODUCTS routes to COMPARE",
        route("COMPARE_PRODUCTS") is Capability.COMPARE,
    )
    check(
        "CHECK_COMPATIBILITY routes to COMPATIBILITY",
        route("CHECK_COMPATIBILITY") is Capability.COMPATIBILITY,
    )
    check(
        "SUGGEST_COMPLEMENTARY routes to COMPLEMENTARY",
        route("SUGGEST_COMPLEMENTARY") is Capability.COMPLEMENTARY,
    )
    check(
        "ADD_TO_CART routes to CART_ACTION (never silently to DISCOVER)",
        route("ADD_TO_CART") is Capability.CART_ACTION,
    )
    check(
        "PREPARE_CHECKOUT routes to CHECKOUT",
        route("PREPARE_CHECKOUT") is Capability.CHECKOUT,
    )
    check(
        "an unrecognized action type routes to UNKNOWN, not DISCOVER",
        route("SOME_MADE_UP_ACTION") is Capability.UNKNOWN,
    )
    check(
        "RETRY_PAYMENT routes to PAYMENT_RECOVERY (Phase 5) - never silently "
        "treated as discovery",
        route("RETRY_PAYMENT") is Capability.PAYMENT_RECOVERY,
    )
    check(
        "OFFER_ALTERNATE_PAYMENT routes to PAYMENT_RECOVERY",
        route("OFFER_ALTERNATE_PAYMENT") is Capability.PAYMENT_RECOVERY,
    )
    check(
        "SPLIT_PAYMENT routes to PAYMENT_RECOVERY",
        route("SPLIT_PAYMENT") is Capability.PAYMENT_RECOVERY,
    )
    check(
        "ISSUE_REFUND (a real, financial ActionType, deliberately not mapped) "
        "routes to UNKNOWN, never silently treated as discovery or as an "
        "executable capability",
        route("ISSUE_REFUND") is Capability.UNKNOWN,
    )

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all capability-routing checks passed")
    print()
    return failures


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
