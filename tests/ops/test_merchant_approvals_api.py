"""M-1 - the merchant-facing approval APIs: GET /api/approvals/{conn} (paged, with
a true total and an executability flag) and GET /api/approvals/{conn}/history.

Model-free and service-free: real FastAPI app in-process, throwaway database,
reject/expire paths only (approve would call a platform adapter).

What this pins down:
  - every approval type is visible to its merchant, not just payment recovery,
    including on a platform with no recovery support (Northfield);
  - tenant isolation and auth, on both routes;
  - `total` is the truth and paging neither repeats nor skips a row;
  - expired-but-unswept approvals never use up a page or inflate the count;
  - the response is a strict superset of the old one (existing callers break on
    nothing);
  - the executability map matches what execution really does, so it cannot drift.

Run from the project root:
    python -m tests.ops.test_merchant_approvals_api
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tests.ops import _support

_support.isolate("merchant_approvals")

import httpx  # noqa: E402

from engine import db  # noqa: E402
from engine.api import approval_view  # noqa: E402
from engine.api.deps import engine as app_engine  # noqa: E402
from engine.api.main import app  # noqa: E402
from engine.db.session import create_schema, dispose  # noqa: E402
from engine.execution.service import ExecutionService  # noqa: E402
from engine.reasoning.prompts import PROPOSABLE  # noqa: E402
from shared.models import ActionType  # noqa: E402

_support.assert_isolated()

C = _support.Checks()

DEMO = "conn_demo"      # Northfield: no payment recovery
KETTLE = "conn_kettle"

#: The fields the existing QueueItem type (storefront/src/api.ts) reads.
OLD_FIELDS = {
    "approval_id", "case_id", "action_type", "risk_rule", "requested_at",
    "expires_at", "friction_type", "diagnosis", "evidence", "used_model",
    "shopper_reply", "model_reply", "selection_reason", "rejected", "financial",
    "order_id", "query",
}


async def seed(conn: str, action: str, *, rule: str, financial: bool, timeout: int = 15):
    return await db.record_case(
        connection_id=conn,
        friction=None,
        query=None,
        cart_id=None,
        order_id=None,
        session_id=None,
        reasoning={"used_model": False, "diagnosis": "d", "evidence": ["e1", "e2"],
                   "shopper_reply": "told"},
        decision={"proposed": [], "rejected": [], "selected_action": action,
                  "selection_reason": "t"},
        risk={"outcome": "HUMAN", "rule": rule, "reason": "t", "financial": financial},
        approval_timeout_minutes=timeout,
    )


async def main() -> int:
    await create_schema()

    operator = await db.keys.mint("operator", label="t-op")
    sk_demo = await db.keys.mint("secret", connection_id=DEMO, label="t-sk-demo")
    sk_kettle = await db.keys.mint("secret", connection_id=KETTLE, label="t-sk-kettle")
    pk_demo = await db.keys.mint("publishable", connection_id=DEMO, label="t-pk-demo")
    revoked = await db.keys.mint("secret", connection_id=DEMO, label="t-revoked")
    for row in await db.keys.list_keys():
        if row["label"] == "t-revoked":
            await db.keys.revoke(row["key_id"])

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:

        async def get(path: str, key: str | None = None):
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            return await http.get(path, headers=headers)

        # -- every approval type is visible, on a platform with no recovery -----
        print("\n  all approval types reach the merchant\n")
        types = {
            "APPLY_PROMOTION": ("FINANCIAL_ALWAYS_HUMAN", True),
            "NOTIFY_BACK_IN_STOCK": ("IRREVERSIBLE_ALWAYS_HUMAN", False),
            "ISSUE_REFUND": ("FINANCIAL_ALWAYS_HUMAN", True),
            "OFFER_ALTERNATE_PAYMENT": ("FINANCIAL_ALWAYS_HUMAN", True),
            "ADD_TO_CART": ("CAUTIOUS_MODE_GATES_ALL", False),
            "PREPARE_CHECKOUT": ("NOT_ON_MERCHANT_ALLOWLIST", False),
        }
        for action, (rule, fin) in types.items():
            await seed(DEMO, action, rule=rule, financial=fin)
        await seed(KETTLE, "RETRY_PAYMENT", rule="FINANCIAL_ALWAYS_HUMAN", financial=True)

        r = await get(f"/api/approvals/{DEMO}", sk_demo)
        body = r.json()
        shown = {a["action_type"] for a in body["approvals"]}
        C.ok("Northfield sees every approval type, not only payment recovery",
             r.status_code == 200 and shown == set(types), str(sorted(shown)))
        C.ok("another merchant's approval is not in the list",
             "RETRY_PAYMENT" not in shown)
        C.ok("total matches the rows when everything fits on one page",
             body["total"] == len(types) == len(body["approvals"]), str(body["total"]))
        first = body["approvals"][0]
        C.ok("the response is a superset of the old QueueItem fields",
             OLD_FIELDS <= set(first), str(OLD_FIELDS - set(first)))
        C.ok("...plus the clocks and the executability flag",
             {"waiting_minutes", "minutes_left", "executable"} <= set(first)
             and isinstance(first["minutes_left"], int) and first["minutes_left"] <= 15)
        C.ok("the old envelope key is unchanged", "approvals" in body)

        exe = {a["action_type"]: a["executable"] for a in body["approvals"]}
        C.ok("back-in-stock and refund are flagged as unable to run, with a reason",
             all(exe[t]["can_run"] is False and exe[t]["reason"]
                 for t in ("NOTIFY_BACK_IN_STOCK", "ISSUE_REFUND")))
        C.ok("the rest are flagged runnable",
             all(exe[t]["can_run"] is True and exe[t]["reason"] is None
                 for t in ("APPLY_PROMOTION", "OFFER_ALTERNATE_PAYMENT",
                           "ADD_TO_CART", "PREPARE_CHECKOUT")))

        # -- auth and tenant isolation ------------------------------------------
        print("\n  auth and tenant isolation\n")
        for path in (f"/api/approvals/{DEMO}", f"/api/approvals/{DEMO}/history"):
            refusals = {
                "no key": (await get(path)).status_code,
                "publishable": (await get(path, pk_demo)).status_code,
                "other merchant": (await get(path, sk_kettle)).status_code,
                "revoked": (await get(path, revoked)).status_code,
                "garbage": (await get(path, "cv3_sk_" + "x" * 64)).status_code,
            }
            C.ok(f"GET {path} refuses every wrong caller with 401",
                 set(refusals.values()) == {401}, str(refusals))
            C.ok(f"GET {path} accepts its own merchant and the operator",
                 (await get(path, sk_demo)).status_code == 200
                 and (await get(path, operator)).status_code == 200)
        via_operator = (await get(f"/api/approvals/{KETTLE}", operator)).json()
        C.ok("the operator key reads one merchant's queue, still scoped to it",
             {a["action_type"] for a in via_operator["approvals"]} == {"RETRY_PAYMENT"})

        # -- expired-but-unswept rows never eat a page or inflate the count -----
        print("\n  expiry: read-time hiding\n")
        for _ in range(6):
            await seed(KETTLE, "OFFER_ALTERNATE_PAYMENT",
                       rule="FINANCIAL_ALWAYS_HUMAN", financial=True, timeout=-5)
        live_before = (await get(f"/api/approvals/{KETTLE}", sk_kettle)).json()["total"]
        C.ok("expired-but-unswept approvals are not counted", live_before == 1,
             f"total={live_before}")
        await seed(KETTLE, "OFFER_ALTERNATE_PAYMENT",
                   rule="FINANCIAL_ALWAYS_HUMAN", financial=True)
        tiny = (await get(f"/api/approvals/{KETTLE}?limit=1&offset=1", sk_kettle)).json()
        C.ok("a small page is filled with live rows, not used up by expired ones",
             len(tiny["approvals"]) == 1 and tiny["total"] == 2, str(tiny["total"]))

        # -- paging --------------------------------------------------------------
        print("\n  paging\n")
        for _ in range(118):
            await seed(KETTLE, "OFFER_ALTERNATE_PAYMENT",
                       rule="FINANCIAL_ALWAYS_HUMAN", financial=True)
        p1 = (await get(f"/api/approvals/{KETTLE}", sk_kettle)).json()
        p2 = (await get(f"/api/approvals/{KETTLE}?offset=50", sk_kettle)).json()
        p3 = (await get(f"/api/approvals/{KETTLE}?offset=100", sk_kettle)).json()
        p4 = (await get(f"/api/approvals/{KETTLE}?offset=1000", sk_kettle)).json()
        C.ok("default page is 50 and total is the truth (120)",
             len(p1["approvals"]) == 50 and p1["total"] == 120 and p1["limit"] == 50,
             f"{len(p1['approvals'])}/{p1['total']}")
        ids = [a["approval_id"] for page in (p1, p2, p3) for a in page["approvals"]]
        C.ok("three pages cover all 120 rows with no repeats or gaps",
             len(ids) == 120 and len(set(ids)) == 120, f"{len(ids)} rows, {len(set(ids))} unique")
        C.ok("a page past the end is empty but still reports the total",
             p4["approvals"] == [] and p4["total"] == 120)
        big = (await get(f"/api/approvals/{KETTLE}?limit=100000", sk_kettle)).json()
        C.ok("an oversized limit is capped, and the response says what it used",
             big["limit"] == 200 and len(big["approvals"]) == 120 and big["total"] == 120)
        neg = (await get(f"/api/approvals/{KETTLE}?limit=-5&offset=-9", sk_kettle)).json()
        C.ok("nonsense limit/offset are clamped rather than erroring",
             neg["limit"] == 1 and neg["offset"] == 0 and len(neg["approvals"]) == 1)
        times = [a["requested_at"] for a in p1["approvals"]]
        C.ok("oldest first", times == sorted(times))

        # -- history ---------------------------------------------------------------
        print("\n  history\n")
        empty = (await get(f"/api/approvals/{DEMO}/history", sk_demo)).json()
        C.ok("a merchant with nothing decided gets an honest empty history",
             empty["decisions"] == [] and empty["total"] == 0, str(empty))

        queue = (await get(f"/api/approvals/{DEMO}", sk_demo)).json()["approvals"]
        victim = next(a for a in queue if a["action_type"] == "APPLY_PROMOTION")
        rej = await http.post(
            f"/api/approvals/{DEMO}/{victim['approval_id']}",
            headers={"Authorization": f"Bearer {sk_demo}"},
            json={"approved": False, "decided_by": "merchant", "note": "not this week"},
        )
        C.ok("a rejection from the merchant's own key goes through",
             rej.status_code == 200 and rej.json()["state"] == "REJECTED", rej.text)
        expired_case = await seed(DEMO, "ADD_TO_CART", rule="CAUTIOUS_MODE_GATES_ALL",
                                  financial=False, timeout=-5)
        C.ok("a pending-but-expired approval is not in history until it is swept",
             all(d["case_id"] != expired_case for d in
                 (await get(f"/api/approvals/{DEMO}/history", sk_demo)).json()["decisions"]))
        swept = await http.post("/api/admin/expire", headers={"Authorization": f"Bearer {operator}"})
        C.ok("the sweep runs", swept.status_code == 200 and swept.json()["expired"] >= 1)

        hist = (await get(f"/api/approvals/{DEMO}/history", sk_demo)).json()
        by_case = {d["case_id"]: d for d in hist["decisions"]}
        C.ok("history holds the rejection and the expiry, and total says so",
             hist["total"] == 2 and victim["case_id"] in by_case and expired_case in by_case,
             str(hist["total"]))
        rj = by_case[victim["case_id"]]
        ex = by_case[expired_case]
        C.ok("the rejection carries who, the note, and what came of it",
             rj["state"] == "REJECTED" and rj["decided_by"] == "merchant"
             and rj["note"] == "not this week" and rj["final_state"] == "REJECTED"
             and rj["resolved"] is False, str(rj))
        C.ok("the expiry is attributed to nobody, and recorded unresolved",
             ex["state"] == "EXPIRED" and ex["decided_by"] == "expired"
             and ex["final_state"] == "TIMEOUT" and ex["resolved"] is False, str(ex))
        C.ok("pending approvals are not in history",
             all(d["state"] != "PENDING" for d in hist["decisions"]))
        C.ok("history is newest first",
             [d["decided_at"] for d in hist["decisions"]]
             == sorted((d["decided_at"] for d in hist["decisions"]), reverse=True))
        C.ok("another merchant's decisions are not in this history",
             all(d["case_id"] in (victim["case_id"], expired_case) for d in hist["decisions"]))
        page = (await get(f"/api/approvals/{DEMO}/history?limit=1&offset=1", sk_demo)).json()
        C.ok("history pages with a true total",
             len(page["decisions"]) == 1 and page["total"] == 2)
        kettle_hist = (await get(f"/api/approvals/{KETTLE}/history", sk_kettle)).json()
        C.ok("Kettle's history is its own (the 6 expired seeds are not swept-and-shown "
             "for Northfield)",
             all(d["case_id"] not in by_case for d in kettle_hist["decisions"]))

    # -- the executability map matches what execution really does -----------------
    print("\n  executability map vs real execution behaviour\n")
    service = ExecutionService(app_engine.registry)

    notify = service._terminal(ActionType.NOTIFY_BACK_IN_STOCK, SimpleNamespace(
        model_reply=None, shopper_reply=None))
    C.ok("NOTIFY_BACK_IN_STOCK really ends UNSUPPORTED when executed",
         notify.succeeded is False and notify.final_state == "UNSUPPORTED",
         f"{notify.final_state}/{notify.succeeded}")
    for action in (ActionType.ISSUE_REFUND, ActionType.CANCEL_ORDER):
        # Unmatched by the dispatcher's match statement, so it falls through to the
        # default without touching the adapter - which is the behaviour under test.
        result = await service._dispatch(None, SimpleNamespace(proposed=[]), action)
        C.ok(f"{action} really has no execution path (falls through to UNSUPPORTED)",
             result.succeeded is False and result.final_state == "UNSUPPORTED",
             f"{result.final_state}/{result.succeeded}")
    C.ok("the map lists exactly those three",
         approval_view.cannot_run_types()
         == {"NOTIFY_BACK_IN_STOCK", "ISSUE_REFUND", "CANCEL_ORDER"})
    C.ok("refund and cancel are not things the model can propose",
         ActionType.ISSUE_REFUND not in PROPOSABLE and ActionType.CANCEL_ORDER not in PROPOSABLE)
    C.ok("every other proposable action is reported as runnable",
         all(approval_view.executability(str(a))["can_run"]
             for a in PROPOSABLE if a is not ActionType.NOTIFY_BACK_IN_STOCK))

    await dispose()
    return C.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
