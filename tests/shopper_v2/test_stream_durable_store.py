"""The durable stream-run record (`db.stream_runs`) and the registry rules built on it (`engine/api/chat_stream.py`): edge cases the HTTP and process tests do not reach.

Disposable database; no services, no model.

    python tests/shopper_v2/test_stream_durable_store.py
"""

from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_stream_durable.db")
for _s in ("", "-journal", "-wal", "-shm"):
    if os.path.exists(_DB + _s):
        os.remove(_DB + _s)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_stream_durable.db"
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402
from datetime import timedelta  # noqa: E402

failures = 0


def check(label: str, ok: bool, detail=None) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + ("" if ok or detail is None else f"   {detail!r}"))
    if not ok:
        failures += 1


async def refusal(coro):
    from fastapi import HTTPException

    try:
        await coro
        return None
    except HTTPException as exc:
        return exc.status_code, exc.detail["code"]


async def main() -> None:
    from engine import db
    from engine.api import chat_stream
    from engine.api.chat_stream import REGISTRY, StreamEvent
    from engine.db import create_schema, dispose

    await create_schema()
    sr = db.stream_runs
    C, S = "conn_demo", "sess_a"
    try:
        print("1. The store: atomic begin, forward-only complete, compaction, purge")
        check("the first begin creates the row", await sr.begin(C, S, "req_00000001", ["own1"], "h1") is True)
        check("a second begin of the same id does not (and does not raise)", await sr.begin(C, S, "req_00000001", ["own2"], "h2") is False)
        row = await sr.get(C, S, "req_00000001")
        check("the original owner and body are kept", row["owner_keys"] == ["own1"] and row["body_hash"] == "h1" and row["status"] == "running", row)
        events = [{"id": 1, "type": "start", "data": {}}, {"id": 2, "type": "delta", "data": {"text": "draft"}}, {"id": 3, "type": "message", "data": {"reply": "ok"}}, {"id": 4, "type": "done", "data": {}}]
        await sr.complete(C, S, "req_00000001", "done", events)
        row = await sr.get(C, S, "req_00000001")
        check("drafts are not stored, original ids are", [e["id"] for e in row["events"]] == [1, 3, 4] and row["status"] == "done", row)
        await sr.complete(C, S, "req_00000001", "error", [{"id": 1, "type": "error", "data": {}}])
        check("a finished row is never rewritten", (await sr.get(C, S, "req_00000001"))["status"] == "done")
        huge = [{"id": 1, "type": "start", "data": {}}, {"id": 2, "type": "status", "data": {"x": "y" * 500_000}}, {"id": 3, "type": "message", "data": {"reply": "r"}}, {"id": 4, "type": "done", "data": {}}]
        await sr.begin(C, S, "req_00000002", ["own1"], "h")
        await sr.complete(C, S, "req_00000002", "done", huge)
        check("an oversized record keeps only start/message/error/done", [e["type"] for e in (await sr.get(C, S, "req_00000002"))["events"]] == ["start", "message", "done"])
        check("the same request id at ANOTHER merchant is a different run", await sr.begin("conn_kettle", S, "req_00000001", ["own1"], "h1") is True)
        check("and at another session", await sr.begin(C, "sess_b", "req_00000001", ["own1"], "h1") is True)
        removed = await sr.purge(timedelta(seconds=-1))
        check("purge forgets rows past retention", removed >= 4 and await sr.get(C, S, "req_00000001") is None, removed)

        print("2. The registry on top of it")
        REGISTRY.clear()
        key = (C, "sess_r", "req_reg00001")
        run, created = await REGISTRY.acquire(key, ["ownA"], "hash1")
        check("a first attempt creates the run and the durable row", created and (await sr.get(*key))["status"] == "running")
        run.emit("start", {})
        run.emit("message", {"reply": "hello"})
        run.emit("done", {})
        await REGISTRY.finish(run)
        check("finish stores 'done'", (await sr.get(*key))["status"] == "done")
        REGISTRY.clear()
        replayed, created2 = await REGISTRY.acquire(key, ["ownA"], "hash1")
        check("after a restart the same owner and body attach to the stored run, not a new one", created2 is False and [e.type for e in replayed.events] == ["start", "message", "done"], [e.type for e in replayed.events])
        check("the rebuilt run is finished, so no turn is started for it", replayed.active is False)
        REGISTRY.clear()
        check("another owner: 404 SESSION_NOT_FOUND", await refusal(REGISTRY.acquire(key, ["ownB"], "hash1")) == (404, "SESSION_NOT_FOUND"))
        check("the same owner, another message: 409 REQUEST_ID_REUSED", await refusal(REGISTRY.acquire(key, ["ownA"], "hash2")) == (409, "REQUEST_ID_REUSED"))

        print("3. A run that ended in an error replays as an error")
        ekey = (C, "sess_e", "req_err00001")
        erun, _ = await REGISTRY.acquire(ekey, ["ownA"], "h")
        erun.emit("start", {})
        erun.emit("error", {"code": "TURN_FAILED", "message": "fixed", "retryable": True})
        erun.emit("done", {})
        await REGISTRY.finish(erun)
        check("stored as 'error'", (await sr.get(*ekey))["status"] == "error")
        REGISTRY.clear()
        again, _ = await REGISTRY.acquire(ekey, ["ownA"], "h")
        check("the retry sees the same fixed error and the turn is not re-run", [e.type for e in again.events] == ["start", "error", "done"] and again.active is False)

        print("4. Interrupted: a run with no outcome is never run again")
        ikey = (C, "sess_i", "req_int00001")
        await REGISTRY.acquire(ikey, ["ownA"], "h")
        REGISTRY.clear()  # the process died: the row stays 'running'
        check("a retry with the same id: 409 TURN_INTERRUPTED", await refusal(REGISTRY.acquire(ikey, ["ownA"], "h")) == (409, "TURN_INTERRUPTED"))
        check("the row is now marked interrupted", (await sr.get(*ikey))["status"] == "interrupted")
        check("and it stays refused", await refusal(REGISTRY.acquire(ikey, ["ownA"], "h")) == (409, "TURN_INTERRUPTED"))
        check("another owner still gets 404, not 409 (no oracle)", await refusal(REGISTRY.acquire(ikey, ["ownB"], "h")) == (404, "SESSION_NOT_FOUND"))
        check("a NEW request id on that session is accepted", (await REGISTRY.acquire((C, "sess_i", "req_int00002"), ["ownA"], "h"))[1] is True)

        print("5. Storage failures fail CLOSED")
        REGISTRY.clear()
        real_begin = sr.begin

        async def broken(*a, **k):
            raise RuntimeError("disk on fire")

        sr.begin = broken
        try:
            res = await refusal(REGISTRY.acquire((C, "sess_f", "req_fail0001"), ["ownA"], "h"))
        finally:
            sr.begin = real_begin
        check("no record, no turn: 503 STREAM_UNAVAILABLE", res == (503, "STREAM_UNAVAILABLE"), res)
        check("and nothing was left in memory or the active-session slot", not REGISTRY._runs and not REGISTRY._active, (REGISTRY._runs, REGISTRY._active))
        real_get = sr.get
        sr.get = broken
        try:
            res = await refusal(REGISTRY.acquire((C, "sess_f", "req_fail0002"), ["ownA"], "h"))
        finally:
            sr.get = real_get
        check("an unreadable record also refuses (503), never assumes 'unseen'", res == (503, "STREAM_UNAVAILABLE"), res)
        frun, _ = await REGISTRY.acquire((C, "sess_f", "req_fail0003"), ["ownA"], "h")
        frun.emit("message", {"reply": "x"})
        real_complete = sr.complete
        sr.complete = broken
        try:
            await REGISTRY.finish(frun)  # must not raise
        finally:
            sr.complete = real_complete
        REGISTRY.clear()
        check("if the outcome could not be stored the row reads as interrupted (the safe reading)", await refusal(REGISTRY.acquire((C, "sess_f", "req_fail0003"), ["ownA"], "h")) == (409, "TURN_INTERRUPTED"))

        print("6. Concurrency: the same id posted twice at once creates one run")
        REGISTRY.clear()
        ckey = (C, "sess_c", "req_conc0001")
        results = await asyncio.gather(*(REGISTRY.acquire(ckey, ["ownA"], "h") for _ in range(8)), return_exceptions=True)
        created_n = sum(1 for r in results if isinstance(r, tuple) and r[1])
        attached = sum(1 for r in results if isinstance(r, tuple) and not r[1])
        check("exactly one caller created it; the others attached to the same run", created_n == 1 and attached == 7 and len({id(r[0]) for r in results}) == 1, (created_n, attached))
        check("one durable row", (await sr.get(*ckey)) is not None)
        REGISTRY.clear()

        print("7. A resume with no record is never started")
        check("Last-Event-ID > 0 for an unknown id: 409 STREAM_RUN_LOST", await refusal(REGISTRY.acquire((C, "sess_x", "req_gone0001"), ["ownA"], "h", resuming=True)) == (409, "STREAM_RUN_LOST"))
        check("and no row or run was created", await sr.get(C, "sess_x", "req_gone0001") is None and not REGISTRY._runs)
    finally:
        await dispose()


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)
