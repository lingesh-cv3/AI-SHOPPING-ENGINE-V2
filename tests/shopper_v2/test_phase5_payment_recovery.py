"""Phase 5 continuation: capability-aware Payment Recovery
(`engine/shopper_v2/payment_recovery.py`).

Uses its own isolated sqlite file (never cv3.db). Requires BOTH demo
merchant backends running (8001 Northfield, 8002 Kettle) - this is the
one V2 test that specifically needs the capability DIFFERENCE between
them, the same core platform-independence claim CLAUDE.md makes for V1.
Does not require the engine (port 8000) itself.

    python tests/shopper_v2/test_phase5_payment_recovery.py
"""

from __future__ import annotations

import os
import sys

_DB_FILE = os.path.join(os.path.dirname(__file__), "..", "..", "_test_shopper_v2_phase5b.db")
if os.path.exists(_DB_FILE):
    os.remove(_DB_FILE)
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./_test_shopper_v2_phase5b.db")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


async def main() -> None:
    from engine.api.deps import build_engine
    from engine.db import create_schema, dispose, get_case

    await create_schema()
    engine = build_engine()

    try:
        # 1. No cart_id -> honest decline, nothing recorded.
        from engine.shopper_v2.payment_recovery import run_payment_recovery_turn

        reply0, summary0 = await run_payment_recovery_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id="conn_demo", session_id="sess_test_pr", cart_id=None,
            message="my payment failed",
        )
        check("no cart_id -> honest decline", summary0 is None and "cart" in reply0.lower())

        # 2. Northfield: declares NO payment-recovery capability at all -
        #    every candidate is rejected by the real capability filter,
        #    DecisionEngine automatically escalates, and (non-financial)
        #    the escalation actually runs.
        nf_adapter = engine.registry.adapter_for("conn_demo")
        nf_cart = await nf_adapter.create_cart()
        reply_nf, summary_nf = await run_payment_recovery_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id="conn_demo", session_id="sess_test_pr_nf", cart_id=nf_cart.cart_id,
            message="my card was declined, please help",
        )
        check("Northfield (no recovery capability) escalates", summary_nf["escalated_because_unsupported"] is True)
        check("Northfield's selected action is ESCALATE_TO_HUMAN", summary_nf["selected_action"] == "ESCALATE_TO_HUMAN")
        check("Northfield's escalation is auto-approved (non-financial)", summary_nf["risk_outcome"] == "AUTO")
        check("Northfield reply names the real capability gap honestly", "can't automatically" in reply_nf.lower() or "passed it to a person" in reply_nf.lower())
        nf_case = await get_case("conn_demo", summary_nf["case_id"])
        check("a real case was recorded for Northfield", nf_case is not None)

        # 3. Kettle: DOES declare payment-recovery capability - a real
        #    recovery action survives the capability filter, but it is
        #    financial, so FINANCIAL_ALWAYS_HUMAN forces HUMAN regardless
        #    of merchant policy, and this module never executes it.
        kb_adapter = engine.registry.adapter_for("conn_kettle")
        kb_cart = await kb_adapter.create_cart()
        reply_kb, summary_kb = await run_payment_recovery_turn(
            decision=engine.decision, gate=engine.gate, execution=engine.execution,
            connection_id="conn_kettle", session_id="sess_test_pr_kb", cart_id=kb_cart.cart_id,
            message="my card was declined, please help",
        )
        check("Kettle (declares recovery capability) does NOT escalate as unsupported", summary_kb["escalated_because_unsupported"] is False)
        check(
            "Kettle's selected action is a real recovery type, not an escalation",
            summary_kb["selected_action"] in ("RETRY_PAYMENT", "OFFER_ALTERNATE_PAYMENT", "SPLIT_PAYMENT"),
        )
        check("Kettle's recovery action is classified HUMAN (financial, never auto)", summary_kb["risk_outcome"] == "HUMAN")
        check("Kettle reply is honest about needing human approval, not a fabricated fix", "person" in reply_kb.lower() or "approve" in reply_kb.lower())
        kb_case = await get_case("conn_kettle", summary_kb["case_id"])
        check("a real case was recorded for Kettle, pending approval", kb_case is not None and kb_case.state == "PENDING_APPROVAL")

        # 4. The two platforms produced genuinely DIFFERENT outcomes for
        #    the identical shopper message - the core platform-
        #    independence claim, now demonstrated through V2 too.
        check(
            "Northfield and Kettle produced different outcomes for the identical message",
            summary_nf["selected_action"] != summary_kb["selected_action"],
        )

        await nf_adapter.close()
        await kb_adapter.close()
    finally:
        await engine.close()
        await dispose()

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all Phase 5 payment-recovery checks passed")
    print()


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(1 if failures else 0)
