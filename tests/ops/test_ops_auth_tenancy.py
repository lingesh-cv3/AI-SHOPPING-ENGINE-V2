"""Phase 0.1 - characterise authentication and tenant isolation on the Operations
and approval routes, against the real FastAPI app, in-process.

No services, no model and no merchant platform are needed: keys are minted into a
throwaway database, the app is driven through an ASGI transport, and the only
approval path exercised is reject/expired - never approve, which would call a
platform adapter.

Deliberately NOT exercised: POST /api/ops/copilot with a valid operator key (that
reaches the model provider). Its refusals are covered; its answers are not, here.

Run from the project root:
    python -m tests.ops.test_ops_auth_tenancy
"""

from __future__ import annotations

import asyncio

from tests.ops import _support

_support.isolate("auth")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from engine import db  # noqa: E402
from engine.api.main import app  # noqa: E402
from engine.db.models import Approval, Case  # noqa: E402
from engine.db.session import create_schema, dispose, session_scope  # noqa: E402

_support.assert_isolated()

C = _support.Checks()

DEMO = "conn_demo"
KETTLE = "conn_kettle"


async def seed(conn: str, *, outcome="HUMAN", timeout=15, final=None) -> str:
    case_id = await db.record_case(
        connection_id=conn,
        friction="PAYMENT_DECLINED",
        query=None,
        cart_id=None,
        order_id=None,
        session_id=None,
        reasoning={"used_model": False, "diagnosis": "d", "evidence": ["e"], "shopper_reply": "s"},
        decision={"proposed": [], "rejected": [], "selected_action": "RETRY_PAYMENT",
                  "selection_reason": "t"},
        risk={"outcome": outcome, "rule": "FINANCIAL_ALWAYS_HUMAN", "reason": "t", "financial": True},
        approval_timeout_minutes=timeout,
    )
    if final:
        await db.record_outcome(connection_id=conn, case_id=case_id, resolved=True, final_state=final)
    return case_id


async def approval_of(case_id: str) -> Approval:
    async with session_scope() as s:
        return (await s.execute(select(Approval).where(Approval.case_id == case_id))).scalar_one()


async def case_of(case_id: str) -> Case:
    async with session_scope() as s:
        return await s.get(Case, case_id)


async def main() -> int:
    await create_schema()

    operator = await db.keys.mint("operator", label="t-operator")
    revoked = await db.keys.mint("operator", label="t-revoked")
    sk_demo = await db.keys.mint("secret", connection_id=DEMO, label="t-sk-demo")
    sk_kettle = await db.keys.mint("secret", connection_id=KETTLE, label="t-sk-kettle")
    pk_demo = await db.keys.mint("publishable", connection_id=DEMO, label="t-pk-demo")
    for row in await db.keys.list_keys():
        if row["label"] == "t-revoked":
            await db.keys.revoke(row["key_id"])

    demo_case = await seed(DEMO)
    kettle_case = await seed(KETTLE)
    demo_apr = (await approval_of(demo_case)).approval_id
    kettle_apr = (await approval_of(kettle_case)).approval_id
    demo_handover = await seed(DEMO, outcome="AUTO", final="ESCALATED")
    kettle_handover = await seed(KETTLE, outcome="AUTO", final="ESCALATED")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:

        async def call(method: str, path: str, key: str | None = None, body=None):
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            return await http.request(method, path, headers=headers, json=body)

        # -- operator routes are operator-only ------------------------------
        print("\n  /api/ops/* and /api/admin/expire: operator key only\n")
        for path in ("/api/ops/queue", "/api/ops/history", "/api/ops/stats", "/api/ops/handovers"):
            refusals = {
                "no key": (await call("GET", path)).status_code,
                "publishable": (await call("GET", path, pk_demo)).status_code,
                "merchant secret": (await call("GET", path, sk_demo)).status_code,
                "other merchant secret": (await call("GET", path, sk_kettle)).status_code,
                "revoked operator": (await call("GET", path, revoked)).status_code,
                "garbage": (await call("GET", path, "cv3_op_" + "x" * 64)).status_code,
            }
            C.ok(f"GET {path} refuses every non-operator caller with 401",
                 set(refusals.values()) == {401}, str(refusals))
            C.ok(f"GET {path} accepts the operator key",
                 (await call("GET", path, operator)).status_code == 200)

        body = {"question": "what is waiting?"}
        C.ok("POST /api/ops/copilot refuses no key, publishable and merchant keys",
             {(await call("POST", "/api/ops/copilot", k, body)).status_code
              for k in (None, pk_demo, sk_demo, sk_kettle)} == {401})
        C.ok("POST /api/admin/expire refuses a merchant key and no key",
             {(await call("POST", "/api/admin/expire", k)).status_code
              for k in (None, sk_demo, pk_demo)} == {401})
        C.ok("POST /api/admin/expire accepts the operator",
             (await call("POST", "/api/admin/expire", operator)).status_code == 200)

        # -- the operator's cross-merchant view ------------------------------
        print("\n  the operator's view spans merchants\n")
        queue = (await call("GET", "/api/ops/queue", operator)).json()["approvals"]
        C.ok("the operator queue contains both merchants' pending items",
             {r["connection_id"] for r in queue} == {DEMO, KETTLE}, str(queue and [r["connection_id"] for r in queue]))
        C.ok("...each with a display name resolved from MERCHANT_NAMES",
             all(r.get("merchant_name") for r in queue))
        handovers = (await call("GET", "/api/ops/handovers", operator)).json()
        C.ok("the operator handover list spans merchants and reports a total",
             {h["connection_id"] for h in handovers["handovers"]} == {DEMO, KETTLE}
             and handovers["total"] == 2)

        # -- handover close: auth + tenancy -----------------------------------
        print("\n  POST /api/ops/handovers/{connection}/{case}\n")
        close_path = f"/api/ops/handovers/{DEMO}/{demo_handover}"
        C.ok("a merchant key cannot close a handover (401), even its own merchant's",
             {(await call("POST", close_path, k, {"handled_by": "x"})).status_code
              for k in (None, pk_demo, sk_demo)} == {401})
        foreign = await call("POST", f"/api/ops/handovers/{KETTLE}/{demo_handover}",
                             operator, {"handled_by": "x"})
        unknown = await call("POST", f"/api/ops/handovers/{KETTLE}/case_does_not_exist",
                             operator, {"handled_by": "x"})
        C.ok("closing a case through the wrong connection changes nothing",
             foreign.status_code == 200 and foreign.json().get("changed") is False, foreign.text)
        C.ok("...and answers exactly as it does for an unknown id (no probing)",
             foreign.json() == {**unknown.json(), "case_id": demo_handover}, f"{foreign.text} / {unknown.text}")
        C.ok("...and the handover is still open", (await case_of(demo_handover)).handled_at is None)
        right = await call("POST", close_path, operator, {"handled_by": "t-op", "note": None})
        C.ok("the right connection closes it", right.json().get("changed") is True, right.text)
        again = await call("POST", close_path, operator, {"handled_by": "t-op"})
        C.ok("closing it again reports changed=False", again.json().get("changed") is False)

        # -- merchant approval routes: tenant isolation ------------------------
        print("\n  /api/approvals and merchant routes: one merchant, one key\n")
        C.ok("a merchant key cannot read another merchant's approval queue",
             (await call("GET", f"/api/approvals/{KETTLE}", sk_demo)).status_code == 401)
        own = (await call("GET", f"/api/approvals/{KETTLE}", sk_kettle)).json()["approvals"]
        C.ok("a merchant key reads its own queue, and only its own",
             [a["approval_id"] for a in own] == [kettle_apr], str(own))
        via_op = (await call("GET", f"/api/approvals/{DEMO}", operator)).json()["approvals"]
        C.ok("the operator key reads a merchant's queue, still scoped to that connection",
             [a["approval_id"] for a in via_op] == [demo_apr])
        C.ok("a publishable key cannot read the approval queue",
             (await call("GET", f"/api/approvals/{DEMO}", pk_demo)).status_code == 401)
        for path in (f"/api/cases/{KETTLE}", f"/api/stats/{KETTLE}", f"/api/report/{KETTLE}"):
            C.ok(f"GET {path} refuses another merchant's key",
                 (await call("GET", path, sk_demo)).status_code == 401)

        # -- decide: tenancy and single decision -------------------------------
        print("\n  POST /api/approvals/{connection}/{id}: tenancy, single decision\n")
        cross = await call("POST", f"/api/approvals/{KETTLE}/{kettle_apr}", sk_demo,
                           {"approved": False, "decided_by": "attacker"})
        C.ok("another merchant's key cannot decide it (401)", cross.status_code == 401)
        C.ok("...and the approval is still PENDING",
             (await approval_of(kettle_case)).state == "PENDING")
        missing = await call("POST", f"/api/approvals/{KETTLE}/apr_nope", sk_kettle,
                             {"approved": False, "decided_by": "x"})
        C.ok("an unknown approval id is 404", missing.status_code == 404)
        rejected = await call("POST", f"/api/approvals/{KETTLE}/{kettle_apr}", sk_kettle,
                              {"approved": False, "decided_by": "someone-else", "note": "n"})
        rj = rejected.json()
        C.ok("the owner's rejection executes nothing",
             rejected.status_code == 200 and rj["state"] == "REJECTED" and rj["executed"] is None, rejected.text)
        stored = await approval_of(kettle_case)
        C.ok(
            "CHARACTERISATION: decided_by is whatever the request body says "
            "(a merchant key recorded as 'someone-else'); identity is not derived from the key",
            stored.decided_by == "someone-else",
            f"stored decided_by={stored.decided_by!r}",
        )
        second = await call("POST", f"/api/approvals/{KETTLE}/{kettle_apr}", sk_kettle,
                            {"approved": True, "decided_by": "x"})
        C.ok("a second decision reports changed=False and executes nothing",
             second.json().get("changed") is False and second.json().get("executed") is None, second.text)

        # -- expired approval pressed from a stale console ---------------------
        expired_case = await seed(KETTLE, timeout=-5)
        expired_apr = (await approval_of(expired_case)).approval_id
        stale = await call("POST", f"/api/approvals/{KETTLE}/{expired_apr}", operator,
                           {"approved": True, "decided_by": "t-op"})
        sj = stale.json()
        C.ok("approving an expired approval runs nothing and says why",
             stale.status_code == 200 and sj.get("expired") is True and sj.get("executed") is None
             and "expired" in (sj.get("reason") or ""), stale.text)
        C.ok("...and the route sweeps it: approval EXPIRED, case TIMEOUT",
             (await approval_of(expired_case)).state == "EXPIRED"
             and (await case_of(expired_case)).state == "TIMEOUT")

        # -- public surface, recorded not judged -------------------------------
        print("\n  public surface\n")
        C.ok("GET /health is public", (await call("GET", "/health")).status_code == 200)
        conns = await call("GET", "/api/connections")
        C.ok(
            "CHARACTERISATION: GET /api/connections is not key-gated "
            "(status is not 401; 200 needs the merchant platforms up, which they are not here)",
            conns.status_code != 401,
            f"status={conns.status_code}",
        )

    await dispose()
    return C.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
