"""Shopper follow-ups: the merchant's own list, detail and close.

Real FastAPI app in-process, throwaway database, no services and no model. None of
these routes calls a platform adapter, which this run proves by working with no
merchant platform running at all.

What this pins down:
  - tenant isolation and auth on all three routes;
  - a true total, paging that neither repeats nor skips, and honest empty results;
  - the detail never crosses merchants: not the conversation (even when two merchants
    share a session id), not the shopper (even when an owner key points at another
    merchant's account);
  - closing is atomic: concurrent closes produce exactly one change, and the actor is
    taken from the authenticated key, never from the request body;
  - a note reaches the shopper verbatim, and no note means nothing is sent;
  - the operator's separate routes keep working, over the same records.

Run from the project root:
    python -m tests.ops.test_merchant_handovers_api
"""

from __future__ import annotations

import asyncio

from tests.ops import _support

_support.isolate("handovers")

import httpx  # noqa: E402
from sqlalchemy import select  # noqa: E402

from engine import db  # noqa: E402
from engine import session as session_store  # noqa: E402
from engine.api.main import app  # noqa: E402
from engine.db.models import Case  # noqa: E402
from adapters.framework.http import PlatformClient  # noqa: E402
from engine.db.session import create_schema, dispose, session_scope  # noqa: E402

_support.assert_isolated()

C = _support.Checks()

DEMO = "conn_demo"
KETTLE = "conn_kettle"


async def escalate(conn: str, *, session_id: str | None = None, order: str | None = None) -> str:
    """A hand-over the way the pipeline leaves one: an ESCALATED case."""
    case_id = await db.record_case(
        connection_id=conn,
        friction="PAYMENT_DECLINED",
        query=None,
        cart_id=None,
        order_id=order,
        session_id=session_id,
        reasoning={"used_model": False, "diagnosis": "card declined", "evidence": ["e1"],
                   "shopper_reply": "someone will be in touch"},
        decision={"proposed": [], "rejected": [], "selected_action": "ESCALATE_TO_HUMAN",
                  "selection_reason": "t"},
        risk={"outcome": "AUTO", "rule": "ESCALATION_ALWAYS_REACHES_A_PERSON",
              "reason": "t", "financial": False},
        approval_timeout_minutes=15,
    )
    await db.record_outcome(connection_id=conn, case_id=case_id, resolved=True,
                            final_state="ESCALATED")
    return case_id


async def say(conn: str, session_id: str, speaker: str, text: str) -> None:
    await session_store.add_turn(session_id=session_id, connection_id=conn,
                                 speaker=speaker, text=text)


async def case_row(case_id: str) -> Case:
    async with session_scope() as s:
        return (await s.execute(select(Case).where(Case.case_id == case_id))).scalar_one()


#: Every call any adapter makes to a merchant platform goes through this one method.
#: Counting it is how the test knows the follow-up routes touched no platform.
platform_calls: list[str] = []
_real_request = PlatformClient.request


async def _counting_request(self, method, path, **kwargs):
    platform_calls.append(f"{method} {path}")
    return await _real_request(self, method, path, **kwargs)


PlatformClient.request = _counting_request


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

    # Shoppers: one of Northfield's, one of Kettle's.
    demo_shopper = await db.shoppers.create(DEMO, "asha", "password123", display_name="Asha",
                                            email="asha@example.com")
    kettle_shopper = await db.shoppers.create(KETTLE, "ravi", "password123", email="ravi@example.com")

    # Northfield: a signed-in shopper with a long conversation, a guest, and a
    # conversation nobody owns (a webhook).
    s1, s2, s3 = "sess-signed-in", "sess-guest", "webhook_abc"
    await db.owners.claim(DEMO, db.owners.SESSION, s1, demo_shopper["shopper_id"])
    await db.owners.claim(DEMO, db.owners.SESSION, s2, "vis_" + "a" * 40)
    for i in range(15):
        await say(DEMO, s1, "shopper" if i % 2 == 0 else "assistant", f"turn-{i:02d}")
    await say(DEMO, s2, "shopper", "guest message")
    h1 = await escalate(DEMO, session_id=s1, order="NF-0001")
    await asyncio.sleep(0.02)
    h2 = await escalate(DEMO, session_id=s2)
    await asyncio.sleep(0.02)
    h3 = await escalate(DEMO, session_id=s3)

    # A case that is not a hand-over, and an approval waiting - neither may be closable.
    not_handover = await db.record_case(
        connection_id=DEMO, friction=None, query=None, cart_id=None, order_id=None,
        session_id=None,
        reasoning={"used_model": False, "diagnosis": "d", "evidence": [], "shopper_reply": "s"},
        decision={"proposed": [], "rejected": [], "selected_action": "RECOMMEND_PRODUCTS",
                  "selection_reason": "t"},
        risk={"outcome": "AUTO", "rule": "AUTO_CLEARED", "reason": "t", "financial": False},
        approval_timeout_minutes=15)

    # Kettle: two hand-overs. One shares a session id with Northfield's (an id is only
    # unique within a merchant), and Northfield's owner claim points at Kettle's shopper
    # for another session to prove an account is never shown across merchants.
    shared = "sess-shared"
    await say(KETTLE, shared, "shopper", "KETTLE-SECRET words")
    await say(DEMO, shared, "shopper", "northfield words")
    await db.owners.claim(DEMO, db.owners.SESSION, shared, kettle_shopper["shopper_id"])
    k1 = await escalate(KETTLE, session_id=shared)
    k2 = await escalate(KETTLE)
    d_shared = await escalate(DEMO, session_id=shared)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:

        async def get(path, key=None):
            return await http.get(path, headers={"Authorization": f"Bearer {key}"} if key else {})

        async def post(path, key=None, body=None):
            return await http.post(path, headers={"Authorization": f"Bearer {key}"} if key else {},
                                   json=body if body is not None else {})

        # -- auth -------------------------------------------------------------
        print("\n  auth and tenant isolation\n")
        paths = [
            ("GET", f"/api/handovers/{DEMO}"),
            ("GET", f"/api/handovers/{DEMO}/{h1}"),
            ("POST", f"/api/handovers/{DEMO}/{h1}/close"),
        ]
        for method, path in paths:
            call = get if method == "GET" else post
            refusals = {
                "no key": (await call(path)).status_code,
                "publishable": (await call(path, pk_demo)).status_code,
                "other merchant": (await call(path, sk_kettle)).status_code,
                "revoked": (await call(path, revoked)).status_code,
                "garbage": (await call(path, "cv3_sk_" + "x" * 64)).status_code,
            }
            C.ok(f"{method} {path.replace(h1, '{case}')} refuses every wrong caller (401)",
                 set(refusals.values()) == {401}, str(refusals))
        C.ok("the merchant's own key reads its list and detail",
             (await get(f"/api/handovers/{DEMO}", sk_demo)).status_code == 200
             and (await get(f"/api/handovers/{DEMO}/{h1}", sk_demo)).status_code == 200)
        C.ok("the operator key passes, still scoped to the addressed merchant",
             {f["case_id"] for f in (await get(f"/api/handovers/{KETTLE}", operator)).json()["follow_ups"]}
             == {k1, k2})

        # -- list ---------------------------------------------------------------
        print("\n  list\n")
        body = (await get(f"/api/handovers/{DEMO}", sk_demo)).json()
        ids = [f["case_id"] for f in body["follow_ups"]]
        C.ok("open follow-ups, oldest first, with a true total",
             ids == [h1, h2, h3, d_shared] and body["total"] == 4, str(ids))
        first = body["follow_ups"][0]
        C.ok("each row carries its waiting age and context",
             isinstance(first["waiting_minutes"], int) and first["order_id"] == "NF-0001"
             and first["friction_type"] == "PAYMENT_DECLINED" and first["evidence"] == ["e1"]
             and first["shopper_reply"] == "someone will be in touch")
        C.ok("the conversation's session id is never put in the list",
             all("session_id" not in f for f in body["follow_ups"]))
        C.ok("a row says whether a conversation exists",
             [f["has_conversation"] for f in body["follow_ups"]] == [True, True, True, True])
        C.ok("another merchant's follow-ups are not in it",
             k1 not in ids and k2 not in ids)
        handled_empty = (await get(f"/api/handovers/{DEMO}?handled=true", sk_demo)).json()
        C.ok("nothing handled yet is an honest empty list, total 0",
             handled_empty["follow_ups"] == [] and handled_empty["total"] == 0)

        # -- detail ---------------------------------------------------------------
        print("\n  detail\n")
        d1 = (await get(f"/api/handovers/{DEMO}/{h1}", sk_demo)).json()
        C.ok("a signed-in shopper of this merchant is identified",
             d1["shopper"]["account"] == {"display_name": "Asha", "username": "asha",
                                          "email": "asha@example.com"}, str(d1["shopper"]))
        C.ok("the conversation is the last 12 turns, in order, text only",
             [t["text"] for t in d1["conversation"]] == [f"turn-{i:02d}" for i in range(3, 15)]
             and all(set(t) == {"speaker", "text", "at"} for t in d1["conversation"]))
        d2 = (await get(f"/api/handovers/{DEMO}/{h2}", sk_demo)).json()
        C.ok("a guest has no contact, and says why",
             d2["shopper"]["account"] is None and "not signed in" in d2["shopper"]["why"])
        d3 = (await get(f"/api/handovers/{DEMO}/{h3}", sk_demo)).json()
        C.ok("an unowned (webhook) conversation has no contact, and says why",
             d3["shopper"]["account"] is None and "no shopper is recorded" in d3["shopper"]["why"])
        ds = (await get(f"/api/handovers/{DEMO}/{d_shared}", sk_demo)).json()
        C.ok("a session id shared with another merchant shows only this merchant's words",
             [t["text"] for t in ds["conversation"]] == ["northfield words"]
             and "KETTLE-SECRET" not in str(ds))
        C.ok("another merchant's account is never shown, even if an owner key points at it",
             ds["shopper"]["account"] is None and "no account of yours" in ds["shopper"]["why"])
        C.ok("a foreign case id is 404 (same as an unknown one)",
             (await get(f"/api/handovers/{DEMO}/{k1}", sk_demo)).status_code == 404
             and (await get(f"/api/handovers/{DEMO}/case_nope", sk_demo)).status_code == 404)
        C.ok("a case that is not a hand-over is 404",
             (await get(f"/api/handovers/{DEMO}/{not_handover}", sk_demo)).status_code == 404)

        # -- close ----------------------------------------------------------------
        print("\n  close\n")
        foreign = await post(f"/api/handovers/{DEMO}/{k1}/close", sk_demo, {"note": "hi"})
        unknown = await post(f"/api/handovers/{DEMO}/case_nope/close", sk_demo, {"note": "hi"})
        C.ok("closing another merchant's case changes nothing, and answers like an unknown id",
             foreign.json() == {**unknown.json(), "case_id": k1}
             and foreign.json()["changed"] is False, f"{foreign.text} / {unknown.text}")
        C.ok("...and it is still open", (await case_row(k1)).handled_at is None)
        C.ok("a case that is not a hand-over cannot be closed",
             (await post(f"/api/handovers/{DEMO}/{not_handover}/close", sk_demo)).json()["changed"] is False
             and (await case_row(not_handover)).handled_at is None)

        big = await post(f"/api/handovers/{DEMO}/{h1}/close", sk_demo, {"note": "x" * 401})
        C.ok("an oversized note is refused, not cut short", big.status_code == 422
             and (await case_row(h1)).handled_at is None)

        ok = await post(f"/api/handovers/{DEMO}/{h1}/close", sk_demo,
                        {"note": "  We have emailed you a payment link.  ", "handled_by": "attacker"})
        C.ok("the merchant closes it and the shopper is told",
             ok.json() == {"case_id": h1, "changed": True, "told_shopper": True}, ok.text)
        closed = await case_row(h1)
        C.ok("who closed it is the authenticated key, not the body",
             closed.handled_by == "secret:t-sk-demo", str(closed.handled_by))
        C.ok("the note is stored trimmed, with a time", closed.handled_note
             == "We have emailed you a payment link." and closed.handled_at is not None)
        turns = await session_store.turns(s1, DEMO, limit=3)
        C.ok("the shopper's conversation now holds the note, verbatim",
             turns[-1]["speaker"] == "assistant"
             and turns[-1]["text"] == "An update from the shop: We have emailed you a payment link.")
        again = await post(f"/api/handovers/{DEMO}/{h1}/close", sk_demo, {"note": "second"})
        C.ok("a second close changes nothing and keeps the first closer and note",
             again.json()["changed"] is False
             and (await case_row(h1)).handled_note == "We have emailed you a payment link.")

        silent = await post(f"/api/handovers/{DEMO}/{h2}/close", operator)
        C.ok("closing with no note is allowed and tells the shopper nothing",
             silent.json() == {"case_id": h2, "changed": True, "told_shopper": False})
        C.ok("...and the operator key is recorded as the operator's",
             (await case_row(h2)).handled_by == "operator:t-op"
             and [t["text"] for t in await session_store.turns(s2, DEMO)] == ["guest message"])
        blank = await post(f"/api/handovers/{DEMO}/{h3}/close", sk_demo, {"note": "   "})
        C.ok("a whitespace-only note counts as no note",
             blank.json() == {"case_id": h3, "changed": True, "told_shopper": False}
             and (await case_row(h3)).handled_note is None)

        # -- concurrency: exactly one close wins ---------------------------------
        print("\n  atomic close\n")
        racers = await asyncio.gather(*[
            post(f"/api/handovers/{DEMO}/{d_shared}/close", key, {"note": f"from {i}"})
            for i, key in enumerate([sk_demo, operator] * 4)
        ])
        winners = [r.json() for r in racers if r.json()["changed"]]
        C.ok("eight simultaneous closes produce exactly one change", len(winners) == 1,
             f"{len(winners)} succeeded")
        final = await case_row(d_shared)
        C.ok("...the recorded actor and note are one closer's, not a mix",
             final.handled_by in ("secret:t-sk-demo", "operator:t-op")
             and final.handled_note in {f"from {i}" for i in range(8)})
        direct = await asyncio.gather(*[
            db.close_handover(KETTLE, k2, f"actor-{i}", f"n{i}") for i in range(20)
        ])
        C.ok("the repository claim itself lets exactly one of 20 callers through",
             sum(r is not None for r in direct) == 1)

        # -- after closing -----------------------------------------------------------
        print("\n  open and handled lists after closing\n")
        open_now = (await get(f"/api/handovers/{DEMO}", sk_demo)).json()
        C.ok("everything on Northfield is closed: honest empty open list",
             open_now["follow_ups"] == [] and open_now["total"] == 0)
        done = (await get(f"/api/handovers/{DEMO}?handled=true", sk_demo)).json()
        C.ok("the handled list holds all four, with who, when and the note",
             done["total"] == 4 and {f["case_id"] for f in done["follow_ups"]} == {h1, h2, h3, d_shared}
             and all(f["handled_at"] and f["handled_by"] for f in done["follow_ups"])
             and all(f["waiting_minutes"] is None for f in done["follow_ups"]))
        times = [f["handled_at"] for f in done["follow_ups"]]
        C.ok("handled is newest first", times == sorted(times, reverse=True))
        kettle_open = (await get(f"/api/handovers/{KETTLE}", sk_kettle)).json()
        C.ok("Kettle's own follow-up (k1) is untouched and still open",
             [f["case_id"] for f in kettle_open["follow_ups"]] == [k1])

        # -- paging ------------------------------------------------------------------
        print("\n  paging\n")
        more = [await escalate(KETTLE) for _ in range(31)]
        p1 = (await get(f"/api/handovers/{KETTLE}", sk_kettle)).json()
        p2 = (await get(f"/api/handovers/{KETTLE}?offset=25", sk_kettle)).json()
        p3 = (await get(f"/api/handovers/{KETTLE}?offset=1000", sk_kettle)).json()
        all_ids = [f["case_id"] for f in p1["follow_ups"]] + [f["case_id"] for f in p2["follow_ups"]]
        C.ok("a page of 25 of 32, and the rest on the next page, total true on both",
             len(p1["follow_ups"]) == 25 and len(p2["follow_ups"]) == 7
             and p1["total"] == p2["total"] == 32 == len({k1, *more}), f"{p1['total']}/{p2['total']}")
        C.ok("no repeats or gaps across pages", len(all_ids) == 32 == len(set(all_ids)))
        C.ok("past the end is empty but still reports the total",
             p3["follow_ups"] == [] and p3["total"] == 32)
        clamp = (await get(f"/api/handovers/{KETTLE}?limit=99999&offset=-4", sk_kettle)).json()
        C.ok("an oversized limit is capped and reported; a negative offset is clamped",
             clamp["limit"] == 200 and clamp["offset"] == 0 and len(clamp["follow_ups"]) == 32)

        # -- the operator's separate routes are unchanged -----------------------------
        print("\n  operator routes over the same records\n")
        ops = (await get("/api/ops/handovers", operator)).json()
        C.ok("the operator's cross-merchant list still sees every open follow-up",
             ops["total"] == 32 and {h["connection_id"] for h in ops["handovers"]} == {KETTLE})
        target = ops["handovers"][0]
        op_close = await post(f"/api/ops/handovers/{KETTLE}/{target['case_id']}", operator,
                              {"handled_by": "op-x", "note": "we called"})
        C.ok("the operator's close still works, takes its actor from the body, and notifies",
             op_close.json() == {"case_id": target["case_id"], "changed": True}
             and (await case_row(target["case_id"])).handled_by == "op-x")
        C.ok("...and a merchant closing lowers the operator's count (one source of truth)",
             (await get("/api/ops/handovers", operator)).json()["total"] == 31)
        C.ok("the operator's close of a non-hand-over changes nothing",
             (await post(f"/api/ops/handovers/{DEMO}/{not_handover}", operator,
                         {"handled_by": "op-x"})).json()["changed"] is False)

    C.ok("none of it called a merchant platform (and none is running here)",
         platform_calls == [], str(platform_calls[:3]))

    await dispose()
    return C.finish()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
