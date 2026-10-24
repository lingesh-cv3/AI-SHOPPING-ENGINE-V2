"""Merchant and operator notification of unresolved payments (model-free).

One alert per unknown-outcome EPISODE, however many taps, retries, sweeps or restarts touch the basket; failures are recorded and
retried, never raised to a shopper's tap; the alert closes itself when the payment is resolved; it carries a reference and reason
codes and nothing payment-sensitive; and it never crosses merchants.

    python tests/shopper_v2/test_payment_alerts.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_pay_alerts.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_pay_alerts.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.dirname(__file__))

import asyncio  # noqa: E402
import json  # noqa: E402

from _pay_fixtures import FakePlatform, FakePlatformWithLookup  # noqa: E402

failures = 0
A, B = "conn_a", "conn_b"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


async def tasks(conn, state=None):
    from engine import db

    return [t for t in await db.list_merchant_tasks(conn, state=state, limit=200) if t["kind"] == "PAYMENT_UNRESOLVED"]


async def pay(platform, cart, card="1111", conn=A):
    from engine.execution import payment_guard

    return await payment_guard.pay_basket(platform, conn, cart, card)


async def age(cart, *, conn=A, unknown=None, started=None):
    from datetime import UTC, datetime, timedelta

    from engine.db import idempotency as ledger
    from engine.db.models import ExecutionAttempt
    from engine.db.session import session_scope

    key = ledger.cart_payment_key(conn, cart)
    async with session_scope() as db:
        row = await db.get(ExecutionAttempt, key)
        res = dict(row.result or {})
        if unknown is not None:
            res["unknown_at"] = (datetime.now(UTC) - timedelta(seconds=unknown)).isoformat()
            res["first_unknown_at"] = res["unknown_at"]  # clocks only: the episode id must not change when time passes
        if started is not None:
            res["started_at"] = (datetime.now(UTC) - timedelta(seconds=started)).isoformat()
        res.pop("recon", None)
        row.result = res


class Registry:
    def __init__(self, platforms):
        self.platforms = platforms

    def adapter_for(self, connection_id):
        return self.platforms.get(connection_id)


async def checks():
    from engine import db
    from engine.db import idempotency as ledger
    from engine.execution import payment_alerts, payment_reconcile as rec

    os.environ["PAYMENT_SETTLE_WINDOW_SECONDS"] = "3600"

    print("1. One alert when a payment becomes unknown")
    p = FakePlatform(["charge_then_timeout"])
    first = await pay(p, "AL-1", "1111")
    t = await tasks(A)
    check("the tap still gets its honest reply (the alert never fails a payment)", first.kind == "unknown", first.kind)
    check("exactly one OPEN merchant task for the basket", len(t) == 1 and t[0]["state"] == "OPEN", t)
    row = await ledger.payment_row(A, "AL-1")
    check("the ledger row records the alert as sent for this episode", row["alert"].get("state") == "sent" and row["alert"].get("episode"), row["alert"])
    d = t[0]["detail"]
    check("the task carries an audit reference (hash of connection+basket), the basket id, the reason code and when it began",
          d["payment_ref"] == payment_alerts.payment_ref(A, "AL-1") and d["cart_id"] == "AL-1" and d["reason"] == "TIMEOUT" and d["since"], d)
    flat = json.dumps(t) + json.dumps(await db.list_merchant_tasks(A, limit=50))
    check("nothing payment-sensitive: no card digits, platform key, order id, amount or platform text",
          not any(x in flat for x in ("1111", "pay-AL-1", "ORD0", "RAW-PLATFORM-SECRET-TEXT", "500", "attempt_keys", "owner")), flat[:300])

    print("\n2. Repeated taps, syncs and sweeps do not alert again")
    for _ in range(3):
        await pay(p, "AL-1", "2222")
    for _ in range(2):
        await payment_alerts.sync(A, "AL-1")
        await payment_alerts.notify(A, "AL-1")
    out = await rec.sweep(Registry({A: p}))
    out2 = await rec.sweep(Registry({A: p}))
    check("still exactly one task after blocked taps, direct syncs and two sweeps", len(await tasks(A)) == 1, await tasks(A))
    check("the sweep reported nothing new raised", out["alerts"]["raised"] == 0 and out2["alerts"]["raised"] == 0, (out, out2))
    check("a direct notify says 'already'", await payment_alerts.notify(A, "AL-1") == "already")

    print("\n3. A notification failure is recorded, retried by the sweep, and never fails the tap")
    real = db.repository.upsert_payment_alert if hasattr(db, "repository") else None
    from engine.db import repository

    real = repository.upsert_payment_alert

    async def boom(*a, **k):
        raise RuntimeError("tasks table unavailable")

    repository.upsert_payment_alert = boom
    try:
        q = FakePlatform(["charge_then_timeout"])
        o = await pay(q, "AL-FAIL", "1111")
        check("the shopper's tap is unaffected by the failing alert", o.kind == "unknown", o.kind)
        r = await ledger.payment_row(A, "AL-FAIL")
        check("the failure is recorded on the row with a try count", r["alert"].get("state") == "failed" and r["alert"].get("tries") == 1, r["alert"])
        await payment_alerts.sync(A, "AL-FAIL")
        r = await ledger.payment_row(A, "AL-FAIL")
        check("each failed attempt counts", r["alert"].get("tries") == 2, r["alert"])
    finally:
        repository.upsert_payment_alert = real
    out = await payment_alerts.sweep()
    r = await ledger.payment_row(A, "AL-FAIL")
    check("the next sweep, with the store back, raises it once", r["alert"].get("state") == "sent" and out["raised"] >= 1 and len([x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-FAIL"]) == 1, (r["alert"], out))
    repository.upsert_payment_alert = boom
    try:
        g = FakePlatform(["charge_then_timeout"])
        await pay(g, "AL-GIVEUP", "1111")
        for _ in range(8):
            await payment_alerts.notify(A, "AL-GIVEUP")
        r = await ledger.payment_row(A, "AL-GIVEUP")
        check("after the retry cap it stops hammering a broken store (the operator list never depends on the alert)", r["alert"].get("tries") == payment_alerts.MAX_ALERT_TRIES and await payment_alerts.notify(A, "AL-GIVEUP") == "gave_up", r["alert"])
    finally:
        repository.upsert_payment_alert = real

    print("\n4. The alert closes itself when the payment is resolved")
    lk = FakePlatformWithLookup(["charge_then_timeout"])
    await pay(lk, "AL-RESOLVE", "1111")
    check("precondition: one open alert for the basket", len([x for x in await tasks(A, "OPEN") if x["detail"]["cart_id"] == "AL-RESOLVE"]) == 1)
    out = await rec.reconcile(lk, A, "AL-RESOLVE", force=True)
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-RESOLVE"]
    check("reconciling to 'paid' closes it, decided by the system, not a person", out["status"] == "paid" and mine and mine[0]["state"] == "RESOLVED" and str(mine[0]["decided_by"]).startswith("system:"), mine)
    op = FakePlatform(["charge_then_timeout"])
    await pay(op, "AL-OP", "1111")
    await age("AL-OP", unknown=7200)
    res = await rec.resolve(op, A, "AL-OP", resolution="not_charged", order_id=None, note="bank confirms")
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-OP"]
    check("an operator's resolution closes it too", res["status"] == "resolved" and mine[0]["state"] == "RESOLVED", (res, mine))
    sk = FakePlatform(["charge_then_timeout", "ok"])
    await pay(sk, "AL-RETRY", "1111")
    await age("AL-RETRY", unknown=7200)
    done = await pay(sk, "AL-RETRY", "1111")
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-RETRY"]
    check("a same-key retry that settles the basket closes it", done.kind == "paid" and mine[0]["state"] == "RESOLVED", (done.kind, mine))

    print("\n5. A later, separate unknown outcome is a new episode with its own alert")
    ep = FakePlatformWithLookup(["charge_then_timeout"])
    await pay(ep, "AL-EP", "1111")
    ep.purchases.clear()
    await age("AL-EP", unknown=7200)
    cleared = await rec.reconcile(ep, A, "AL-EP", force=True)
    ep.script.append("charge_then_timeout")
    await pay(ep, "AL-EP", "2222")
    eps = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-EP"]
    check("cleared, then unknown again: two tasks, the first resolved and the second open", cleared["status"] == "cleared" and sorted(x["state"] for x in eps) == ["OPEN", "RESOLVED"], [x["state"] for x in eps])
    rt = FakePlatform(["charge_then_timeout", "charge_then_timeout"])
    await pay(rt, "AL-SAMEEP", "1111")
    await age("AL-SAMEEP", unknown=7200)
    await pay(rt, "AL-SAMEEP", "1111")
    check("a same-key retry that fails again stays the SAME episode: still one task after it parked the basket unknown again", len([x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-SAMEEP"]) == 1, [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-SAMEEP"])

    print("\n6. Stale and orphaned attempts are alerted by the sweep")
    await ledger.begin_payment(A, "AL-ORPHAN", attempt_key="pay-AL-ORPHAN-1111")
    check("an IN_FLIGHT row alone raises nothing", not [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-ORPHAN"])
    await ledger.stale_in_flight_to_unknown(0)  # what engine startup does
    check("parked by the startup sweep but not yet alerted", not [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-ORPHAN"])
    out = await rec.sweep(Registry({A: FakePlatform()}))
    check("the next payment sweep raises it", len([x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-ORPHAN"]) == 1 and out["alerts"]["raised"] >= 1, out)

    print("\n7. The sweep closes an alert whose basket is no longer unknown")
    await ledger.payment_settled(A, "AL-ORPHAN", succeeded=False, order_id=None, summary="resolved elsewhere",
                                 token=(await ledger.payment_row(A, "AL-ORPHAN"))["token"], from_unknown=True)
    out = await payment_alerts.sweep()
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-ORPHAN"]
    check("settled behind its back -> the next sweep closes the stale alert", mine[0]["state"] == "RESOLVED" and out["closed"] >= 1, (mine, out))

    print("\n8. A merchant cannot 'resolve' a payment alert")
    open_t = (await tasks(A, "OPEN"))[0]
    res = await db.decide_merchant_task(A, open_t["task_id"], resolved=True, decided_by="merchant-user")
    again = [x for x in await tasks(A) if x["task_id"] == open_t["task_id"]][0]
    check("decide refuses: unchanged and still open (the basket has not changed)", res["changed"] is False and again["state"] == "OPEN", (res, again))

    print("\n9. Tenant isolation")
    pa = FakePlatform(["charge_then_timeout"])
    pb = FakePlatform(["charge_then_timeout"])
    await pay(pa, "SAME-ID", "1111", conn=A)
    before_b = len(await tasks(B))
    await pay(pb, "SAME-ID", "1111", conn=B)
    ta = [x for x in await tasks(A) if x["detail"]["cart_id"] == "SAME-ID"]
    tb = [x for x in await tasks(B) if x["detail"]["cart_id"] == "SAME-ID"]
    check("the same cart id under two merchants raises one task for EACH, each in its own merchant's list", len(ta) == 1 and len(tb) == 1 and before_b == 0, (len(ta), len(tb), before_b))
    check("their references differ", ta[0]["detail"]["payment_ref"] != tb[0]["detail"]["payment_ref"])
    check("merchant B never sees merchant A's tasks", all(x["detail"]["cart_id"] == "SAME-ID" for x in await tasks(B)) and len(await tasks(B)) == 1)
    stats_a = await db.ops_stats([A])
    stats_b = await db.ops_stats([B])
    stats_none = await db.ops_stats([])
    stats_both = await db.ops_stats([A, B])
    check("operator figures are scoped to the merchants asked about", stats_b["unresolved_payments"] == 1 and stats_a["unresolved_payments"] >= 2
          and stats_both["unresolved_payments"] == stats_a["unresolved_payments"] + stats_b["unresolved_payments"], (stats_a, stats_b, stats_both))
    check("zero data is an honest zero", stats_none["unresolved_payments"] == 0 and stats_none["oldest_unresolved_payment_minutes"] == 0, stats_none)
    await age("SAME-ID", conn=A, unknown=3 * 3600)
    check("the oldest held payment's age is reported in minutes", (await db.ops_stats([A]))["oldest_unresolved_payment_minutes"] >= 179, await db.ops_stats([A]))

    print("\n10. A same-key retry in flight keeps the alert open, and ending unknown again does not lose it")
    rp = FakePlatform(["charge_then_timeout"])
    await pay(rp, "AL-INFLIGHT", "1111")
    await age("AL-INFLIGHT", unknown=7200)
    retry = await ledger.retry_unknown(A, "AL-INFLIGHT", attempt_key="pay-AL-INFLIGHT-1111")
    row = await ledger.payment_row(A, "AL-INFLIGHT")
    check("the retry moved the row to IN_FLIGHT and kept its alert bookkeeping and episode", retry and row["state"] == "IN_FLIGHT" and row["alert"].get("state") == "sent" and row["episode"], row)
    out = await payment_alerts.sweep()
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-INFLIGHT"]
    check("a sweep while the retry is in flight does NOT close the alert (the basket is still unresolved)", len(mine) == 1 and mine[0]["state"] == "OPEN", (mine, out))
    await ledger.mark_unknown(A, "AL-INFLIGHT", token=retry["token"], reason="TIMEOUT")
    await payment_alerts.sync(A, "AL-INFLIGHT")
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-INFLIGHT"]
    check("the retry ended unknown again: still exactly one task for the basket, and it is open", len(mine) == 1 and mine[0]["state"] == "OPEN", mine)
    await ledger.payment_settled(A, "AL-INFLIGHT", succeeded=False, order_id=None, summary="x", token=(await ledger.payment_row(A, "AL-INFLIGHT"))["token"], from_unknown=True)
    await payment_alerts.sweep()
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-INFLIGHT"]
    check("and once the basket really is settled the sweep closes it", mine[0]["state"] == "RESOLVED", mine)

    print("\n11. An alert the SYSTEM closed too early is reopened; one a person decided is not")
    ro = FakePlatform(["charge_then_timeout"])
    await pay(ro, "AL-REOPEN", "1111")
    ref = payment_alerts.payment_ref(A, "AL-REOPEN")
    await repository.close_payment_alerts(A, f"{ref}:", decided_by="system: closed in error")
    # forget that it was alerted so notify runs the whole path again
    row = await ledger.payment_row(A, "AL-REOPEN")
    await ledger.record_alert(A, "AL-REOPEN", episode="other", state="failed", token=row["token"])
    result = await payment_alerts.notify(A, "AL-REOPEN")
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-REOPEN"]
    check("notify on a still-unknown basket reopens the system-closed task of the same episode (one task, open)", result == "sent" and len(mine) == 1 and mine[0]["state"] == "OPEN" and mine[0]["decided_by"] is None, (result, mine))
    rq = FakePlatform(["charge_then_timeout"])
    await pay(rq, "AL-PERSON", "1111")
    ref = payment_alerts.payment_ref(A, "AL-PERSON")
    await repository.close_payment_alerts(A, f"{ref}:", decided_by="a person at the shop")
    row = await ledger.payment_row(A, "AL-PERSON")
    await ledger.record_alert(A, "AL-PERSON", episode="other", state="failed", token=row["token"])
    await payment_alerts.notify(A, "AL-PERSON")
    mine = [x for x in await tasks(A) if x["detail"]["cart_id"] == "AL-PERSON"]
    check("a task a person closed is left closed", len(mine) == 1 and mine[0]["state"] == "RESOLVED", mine)

    print("\n12. Bookkeeping cannot downgrade a sent alert")
    row = await ledger.payment_row(A, "AL-1")
    ep = row["alert"].get("episode")
    await ledger.record_alert(A, "AL-1", episode=ep, state="failed", token=row["token"])
    after = await ledger.payment_row(A, "AL-1")
    check("a late 'failed' write for the same episode does not overwrite 'sent'", after["alert"].get("state") == "sent" and after["alert"].get("tries") == row["alert"].get("tries"), (row["alert"], after["alert"]))

    print("\n13. The sweep pages through more unresolved baskets than one page")
    repository.upsert_payment_alert = boom
    try:
        for i in range(5):
            pq = FakePlatform(["charge_then_timeout"])
            await pay(pq, f"AL-PAGE-{i}", "1111", conn=B)
    finally:
        repository.upsert_payment_alert = real
    check("precondition: five baskets are unknown with a failed (not sent) alert", all([(await ledger.payment_row(B, f"AL-PAGE-{i}"))["alert"].get("state") == "failed" for i in range(5)]))
    old_page = payment_alerts._PAGE
    payment_alerts._PAGE = 2
    try:
        out = await payment_alerts.sweep()
    finally:
        payment_alerts._PAGE = old_page
    mine_b = [x for x in await tasks(B) if x["detail"]["cart_id"].startswith("AL-PAGE-")]
    check("one sweep with a page size of 2 raised all five (nothing silently beyond the first page)", len(mine_b) == 5 and all(x["state"] == "OPEN" for x in mine_b), (out, len(mine_b)))


async def main():
    from engine.db import create_schema, dispose

    await create_schema()
    try:
        await checks()
    finally:
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
