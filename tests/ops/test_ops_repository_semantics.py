"""Phase 0.1 - characterise the repository semantics the Operations console rests on.

Covers the cross-merchant queue, approvals, expiry, handovers and workload counts
(engine/db/repository.py), at repository level, against a throwaway database.

Nothing here changes product behaviour. Where the code is wrong today, the test
asserts the CORRECT behaviour as a DEFECT line and prints what it really does.

Run from the project root:
    python -m tests.ops.test_ops_repository_semantics
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from tests.ops import _support

_support.isolate("repository")

from sqlalchemy import select  # noqa: E402

from engine import db  # noqa: E402
from engine.db.models import Approval, Case, Outcome  # noqa: E402
from engine.db.session import create_schema, dispose, session_scope  # noqa: E402

_support.assert_isolated()

C = _support.Checks()


async def seed(
    conn: str,
    *,
    friction: str = "PAYMENT_DECLINED",
    outcome: str = "HUMAN",
    timeout: int = 15,
    final: str | None = None,
    order: str | None = None,
) -> str:
    """One case the way the pipeline writes it. HUMAN -> a PENDING approval."""
    case_id = await db.record_case(
        connection_id=conn,
        friction=friction,
        query=None,
        cart_id=None,
        order_id=order,
        session_id=None,
        reasoning={
            "used_model": False,
            "diagnosis": "test diagnosis",
            "evidence": ["test evidence"],
            "shopper_reply": "test reply",
        },
        decision={
            "proposed": [],
            "rejected": [],
            "selected_action": "RETRY_PAYMENT",
            "selection_reason": "test",
        },
        risk={
            "outcome": outcome,
            "rule": "FINANCIAL_ALWAYS_HUMAN",
            "reason": "test",
            "financial": True,
        },
        approval_timeout_minutes=timeout,
    )
    if final:
        await db.record_outcome(
            connection_id=conn, case_id=case_id, resolved=True, final_state=final
        )
    return case_id


async def approval_id_for(case_id: str) -> str:
    async with session_scope() as s:
        row = await s.execute(select(Approval).where(Approval.case_id == case_id))
        return row.scalar_one().approval_id


async def approval_row(approval_id: str) -> Approval:
    async with session_scope() as s:
        return await s.get(Approval, approval_id)


async def case_row(case_id: str) -> Case:
    async with session_scope() as s:
        return await s.get(Case, case_id)


async def main() -> int:
    await create_schema()

    # -- cross-merchant queue: scoping, ordering, clocks ----------------------
    print("\n  pending_across: tenant scoping, ordering, clocks\n")
    a1 = await seed("ops_A")
    a2 = await seed("ops_A")
    b1 = await seed("ops_B")

    rows_a = await db.pending_across(["ops_A"])
    rows_b = await db.pending_across(["ops_B"])
    rows_ab = await db.pending_across(["ops_A", "ops_B"])
    C.ok("one merchant's id list returns only its items",
         {r["connection_id"] for r in rows_a} == {"ops_A"} and len(rows_a) == 2)
    C.ok("the other merchant sees only its own", len(rows_b) == 1 and rows_b[0]["case_id"] == b1)
    C.ok("a list of both returns both", len(rows_ab) == 3)
    C.ok("an empty id list returns nothing, not everything", await db.pending_across([]) == [])
    C.ok("an unknown connection returns nothing", await db.pending_across(["ops_nobody"]) == [])
    C.ok("oldest first", [r["case_id"] for r in rows_ab] == [a1, a2, b1],
         str([r["case_id"] for r in rows_ab]))
    first = rows_a[0]
    C.ok("a waiting clock and a time-left clock are reported",
         isinstance(first["waiting_minutes"], int) and first["waiting_minutes"] >= 0
         and first["minutes_left"] is not None and first["minutes_left"] <= 15)

    # -- expired but not yet swept --------------------------------------------
    print("\n  expiry: read-time hiding, decide guard, sweep\n")
    e_case = await seed("ops_E", timeout=-5)
    e_apr = await approval_id_for(e_case)
    C.ok("an expired-but-unswept approval is hidden from pending_across",
         await db.pending_across(["ops_E"]) == [])
    stats_e = await db.ops_stats(["ops_E"])
    C.ok("...and not counted as waiting", stats_e["waiting"] == 0, str(stats_e))
    C.ok("...but its row is still PENDING until a sweep runs",
         (await approval_row(e_apr)).state == "PENDING")

    decided = await db.decide_approval("ops_E", e_apr, approved=True, decided_by="t")
    C.ok("deciding an expired approval is refused and flagged expired",
         decided is not None and decided.get("expired") is True and decided.get("changed") is False,
         str(decided))
    C.ok("...and leaves it PENDING for the sweeper (so the shopper is told once)",
         (await approval_row(e_apr)).state == "PENDING")

    swept = await db.expire_approvals()
    C.ok("the sweep reports exactly the expired approval",
         [s["approval_id"] for s in swept] == [e_apr], str(swept))
    row = await approval_row(e_apr)
    C.ok("...marking it EXPIRED, decided_by 'expired'",
         row.state == "EXPIRED" and row.decided_by == "expired")
    C.ok("...and moving its case to TIMEOUT", (await case_row(e_case)).state == "TIMEOUT")
    C.ok("a second sweep finds nothing (idempotent)", await db.expire_approvals() == [])

    # -- zero data ---------------------------------------------------------
    print("\n  zero data\n")
    C.ok("ops_stats([]) is an honest zero",
         await db.ops_stats([]) == {"waiting": 0, "oldest_wait_minutes": 0, "by_merchant": {}, "today": 0})
    z = await db.ops_stats(["ops_empty"])
    C.ok("ops_stats for a merchant with no cases is an honest zero",
         z["waiting"] == 0 and z["oldest_wait_minutes"] == 0 and z["by_merchant"] == {} and z["today"] == 0,
         str(z))
    C.ok("handovers_across([]) is ([], 0)", await db.handovers_across([]) == ([], 0))
    fs = await db.friction_summary_across(["ops_empty"])
    C.ok("friction summary for a merchant with no cases is empty, not an error",
         fs["by_type"] == [] and fs["by_type_by_merchant"] == {})
    C.ok("friction summary for [] is empty",
         (await db.friction_summary_across([]))["by_type"] == [])

    # -- decide_approval ---------------------------------------------------
    print("\n  decide_approval: tenant scoping, single decision\n")
    d_case = await seed("ops_D1")
    d_apr = await approval_id_for(d_case)
    C.ok("another merchant's id cannot decide it (returns None)",
         await db.decide_approval("ops_D2", d_apr, approved=True, decided_by="t") is None)
    C.ok("...and nothing changed", (await approval_row(d_apr)).state == "PENDING")
    first_decision = await db.decide_approval(
        "ops_D1", d_apr, approved=False, decided_by="someone", note="n")
    C.ok("the owner's rejection is recorded", first_decision["changed"] is True
         and first_decision["state"] == "REJECTED")
    C.ok("...and moves the case to REJECTED", (await case_row(d_case)).state == "REJECTED")
    second = await db.decide_approval("ops_D1", d_apr, approved=True, decided_by="other")
    C.ok("a second decision does not overwrite the first",
         second["changed"] is False and second["state"] == "REJECTED", str(second))
    C.ok("...and the recorded decider is still the first",
         (await approval_row(d_apr)).decided_by == "someone")

    # -- get_case / record_outcome scoping ------------------------------------
    print("\n  case access: tenant scoping\n")
    C.ok("get_case with another merchant's connection returns None",
         await db.get_case("ops_B", a1) is None)
    C.ok("get_case with the right connection returns it",
         (await db.get_case("ops_A", a1)).case_id == a1)
    await db.record_outcome(connection_id="ops_B", case_id=a1, resolved=True, final_state="OUTCOME")
    async with session_scope() as s:
        leaked = (await s.execute(select(Outcome).where(Outcome.case_id == a1))).scalar_one_or_none()
    C.ok("record_outcome from another merchant writes nothing", leaked is None)
    C.ok("...and does not move the case", (await case_row(a1)).state == "PENDING_APPROVAL")

    # -- handovers ---------------------------------------------------------
    print("\n  handovers: scoping, paging, close\n")
    h1 = [await seed("ops_H1", outcome="AUTO", final="ESCALATED") for _ in range(3)]
    h2 = await seed("ops_H2", outcome="AUTO", final="ESCALATED")
    rows, total = await db.handovers_across(["ops_H1"])
    C.ok("handovers are scoped to the given merchants", total == 3 and {r["case_id"] for r in rows} == set(h1))
    C.ok("oldest first", [r["case_id"] for r in rows] == h1)
    page, total_p = await db.handovers_across(["ops_H1"], limit=2, offset=0)
    page2, total_p2 = await db.handovers_across(["ops_H1"], limit=2, offset=2)
    C.ok("a page reports the true total, not the page size",
         len(page) == 2 and total_p == 3 and len(page2) == 1 and total_p2 == 3,
         f"{len(page)}/{total_p}, {len(page2)}/{total_p2}")
    _, total_both = await db.handovers_across(["ops_H1", "ops_H2"])
    C.ok("a list of both merchants counts both", total_both == 4)

    C.ok("closing with another merchant's connection does nothing (None)",
         await db.mark_handled("ops_H2", h1[0], "op") is None)
    C.ok("...and the handover is still open", (await db.handovers_across(["ops_H1"]))[1] == 3)
    C.ok("closing an unknown case id returns None",
         await db.mark_handled("ops_H1", "case_does_not_exist", "op") is None)
    closed = await db.mark_handled("ops_H1", h1[0], "operator-x", "sent a link")
    C.ok("the owner closes it and gets the session id back for the shopper message",
         closed is not None and closed["case_id"] == h1[0] and "session_id" in closed)
    C.ok("closing it again returns None (no double close)",
         await db.mark_handled("ops_H1", h1[0], "op") is None)
    closed_case = await case_row(h1[0])
    C.ok("who, when and what is recorded on the case",
         closed_case.handled_by == "operator-x" and closed_case.handled_at is not None
         and closed_case.handled_note == "sent a link")
    C.ok("an open handover count drops by one", (await db.handovers_across(["ops_H1"]))[1] == 2)
    C.ok("a closed handover keeps state ESCALATED (open-ness is the handled_at field)",
         closed_case.state == "ESCALATED")

    # -- history -----------------------------------------------------------
    print("\n  decided_across\n")
    hist = await db.decided_across(["ops_D1", "ops_E"])
    by_apr = {h["approval_id"]: h for h in hist}
    C.ok("history includes both a human decision and an expiry", d_apr in by_apr and e_apr in by_apr)
    C.ok("an expiry is attributed to 'expired'", by_apr[e_apr]["decided_by"] == "expired")
    C.ok("history is scoped", await db.decided_across(["ops_A"]) == [])

    # -- friction ----------------------------------------------------------
    print("\n  friction_summary_across\n")
    await seed("ops_F1", friction="DEAD_SEARCH", outcome="AUTO")
    await seed("ops_F1", friction="DEAD_SEARCH", outcome="AUTO")
    await seed("ops_F2", friction="PAYMENT_DECLINED", outcome="AUTO")
    f1 = await db.friction_summary_across(["ops_F1"])
    C.ok("friction is counted per real category and scoped",
         f1["by_type"] == [{"type": "DEAD_SEARCH", "count": 2}]
         and list(f1["by_type_by_merchant"]) == ["ops_F1"], str(f1))

    # -- known defect: "decided today" ------------------------------------
    print("\n  workload counts\n")
    await seed("ops_T", outcome="HUMAN")
    t_reject = await seed("ops_T", outcome="HUMAN")
    t_expire = await seed("ops_T", outcome="HUMAN", timeout=-5)
    await db.decide_approval(
        "ops_T", await approval_id_for(t_reject), approved=False, decided_by="human")
    await db.expire_approvals()
    today = (await db.ops_stats(["ops_T"]))["today"]
    C.defect(
        "'decided today' counts only decisions a person made (1 rejection, 1 expiry -> 1)",
        today == 1,
        f"today={today}; the expired approval (decided_by='expired') is counted as a decision",
    )

    # -- characterisation: the queue cap and the stats count disagree ---------
    cap_ids = [await seed("ops_CAP") for _ in range(105)]
    listed = await db.pending_across(["ops_CAP"])
    counted = (await db.ops_stats(["ops_CAP"]))["waiting"]
    C.ok(
        "CHARACTERISATION: pending_across silently caps at 100 and returns no total",
        isinstance(listed, list) and len(listed) == 100,
        f"len={len(listed)}",
    )
    C.ok(
        "CHARACTERISATION: ops_stats counts all 105 while the list shows 100 "
        "(queue and headline disagree at volume)",
        counted == 105 and len(listed) == 100 and len(cap_ids) == 105,
        f"waiting={counted}, listed={len(listed)}",
    )

    # -- known defect: duplicate function ------------------------------------
    src = Path(_support.ROOT / "engine" / "db" / "repository.py").read_text(encoding="utf-8")
    defs = [
        n.lineno for n in ast.parse(src).body
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "expire_approvals"
    ]
    C.defect(
        "expire_approvals is defined once in repository.py",
        len(defs) == 1,
        f"defined {len(defs)} times at lines {defs}",
    )

    await dispose()
    return C.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
