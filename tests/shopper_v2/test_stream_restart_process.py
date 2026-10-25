"""Duplicate-turn protection across a REAL engine process restart (the engine process is killed and started again on the same disposable database).

What this proves, with a real process boundary (not a cleared dict):
  1. a turn that FINISHED before the kill is replayed from the durable record after the restart - same reply, the cart changed once, the transcript has one entry;
  2. a turn that was RUNNING when the engine was killed is refused TURN_INTERRUPTED when its request id is sent again (never run again), while a NEW request id works;
  3. another visitor, and the same ids under another message, are refused after the restart (isolation and REQUEST_ID_REUSED survive it);
  4. two simultaneous posts of one request id run the turn once.
Model-free for the add; the comparison uses the local fake provider (tests/support/fake_openai_provider.py) so there is a turn slow enough to kill mid-way. No real provider,
no payment. Needs the demo merchants (8001/8002): run it as  python <harness>/with_merchants.py python tests/shopper_v2/test_stream_restart_process.py

Uses its own disposable database and ports 18000 (engine) and 18998 (fake provider); never cv3.db.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_FILE = r"D:\CV3\stack_scratch\restart_process_test.db"
os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
for _s in ("", "-journal", "-wal", "-shm"):
    if os.path.exists(DB_FILE + _s):
        os.remove(DB_FILE + _s)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///" + DB_FILE.replace("\\", "/")  # BEFORE any engine import
assert "cv3.db" not in os.environ["DATABASE_URL"] and "engine" not in sys.modules
sys.path.insert(0, REPO)

import asyncio  # noqa: E402
import json  # noqa: E402
import socket  # noqa: E402

import httpx  # noqa: E402

failures = 0
CONN = "conn_demo"
ENGINE_PORT, PROVIDER_PORT = 18000, 18998
BASE = f"http://127.0.0.1:{ENGINE_PORT}"
PROVIDER = f"http://127.0.0.1:{PROVIDER_PORT}"


def check(label: str, ok: bool, detail=None) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + ("" if ok or detail is None else f"   {detail!r}"))
    if not ok:
        failures += 1


def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def wait_port(port: int, secs: float = 60) -> bool:
    end = time.time() + secs
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(0.4)
    return False


def spawn_engine() -> subprocess.Popen:
    env = dict(os.environ, LLM_PROVIDER="openai", OPENAI_API_KEY="fake-key-local-fake-provider", OPENAI_BASE_URL=f"{PROVIDER}/v1",
               GROQ_API_KEY="", MAILER_SMTP_HOST="", NO_PROXY="127.0.0.1,localhost")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "engine.api.main:app", "--port", str(ENGINE_PORT), "--log-level", "warning"],
                            cwd=REPO, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_port(ENGINE_PORT), "the engine did not start"
    return proc


def kill(proc: subprocess.Popen) -> None:
    """Forceful on purpose: a crash, not a shutdown. Safe here because the database is this test's own disposable file."""
    proc.kill()
    proc.wait(timeout=20)
    for _ in range(50):
        if not port_open(ENGINE_PORT):
            return
        time.sleep(0.2)


def claim(**kw):
    base = {"capability": "discover", "context_action": "continue", "question_type": "retrieve", "reference_scope": "none", "reference_kind": "none",
            "conversational_repair": "none", "cart_operation": "none", "rationale": "scripted"}
    base.update(kw)
    return base


class Stream:
    def __init__(self):
        self.events, self.status = [], None

    def types(self):
        return [e["event"] for e in self.events]

    def of(self, t):
        return [e for e in self.events if e["event"] == t]


async def post_stream(client: httpx.AsyncClient, pk: str, body: dict, *, last_event_id: int | None = None, stop_after_types: set[str] | None = None) -> Stream:
    out = Stream()
    headers = {"Authorization": f"Bearer {pk}", "Accept": "text/event-stream"}
    if last_event_id:
        headers["Last-Event-ID"] = str(last_event_id)
    try:
        async with client.stream("POST", f"{BASE}/api/v2/chat/stream", json=body, headers=headers, timeout=120) as r:
            out.status = r.status_code
            if r.status_code != 200:
                await r.aread()
                return out
            block: list[str] = []
            async for line in r.aiter_lines():
                if line:
                    block.append(line)
                    continue
                ev = {"event": "message", "id": None, "data": None}
                for ln in block:
                    if ln.startswith(":"):
                        continue
                    k, _, v = ln.partition(":")
                    v = v.lstrip(" ")
                    if k == "event":
                        ev["event"] = v
                    elif k == "id":
                        ev["id"] = int(v)
                    elif k == "data":
                        ev["data"] = json.loads(v)
                block = []
                if ev["data"] is not None:
                    out.events.append(ev)
                    if stop_after_types and ev["event"] in stop_after_types:
                        return out
    except (httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectError):
        pass  # the engine was killed under this read
    return out


def body_for(session: str, message: str, request_id: str, cart_id: str | None) -> dict:
    d = {"connection_id": CONN, "session_id": session, "message": message, "request_id": request_id}
    if cart_id:
        d["cart_id"] = cart_id
    return d


async def main() -> None:
    from engine import db
    from engine.db import create_schema, dispose

    await create_schema()
    pk = await db.keys.mint("publishable", connection_id=CONN, label="restart-test")
    await dispose()

    provider = subprocess.Popen([sys.executable, os.path.join(REPO, "tests", "support", "fake_openai_provider.py"), "--port", str(PROVIDER_PORT)],
                                cwd=REPO, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert wait_port(PROVIDER_PORT), "the fake provider did not start"
    eng = spawn_engine()
    H = {"Authorization": f"Bearer {pk}"}

    async def script(**data):
        async with httpx.AsyncClient() as c:
            return (await c.post(f"{PROVIDER}/__script", json=data)).json()

    async def new_cart(c: httpx.AsyncClient) -> str:
        r = await c.post(f"{BASE}/api/shop/{CONN}/cart", headers=H, timeout=60)
        return r.json()["cart_id"]

    async def qty(c: httpx.AsyncClient, cart: str) -> int:
        r = await c.get(f"{BASE}/api/shop/{CONN}/cart/{cart}", headers=H, timeout=60)
        return sum(ln["quantity"] for ln in r.json().get("lines", []))

    async def transcript(c: httpx.AsyncClient, session: str) -> list[dict]:
        r = await c.get(f"{BASE}/api/chat/{CONN}/{session}", headers=H, timeout=60)
        return r.json().get("turns", [])

    try:
        print("1. A turn that finished before the kill is replayed after the restart - never run again")
        ann = httpx.AsyncClient()
        ses1 = f"sess_{uuid.uuid4().hex[:10]}"
        cart1 = await new_cart(ann)
        rid1 = f"req_{uuid.uuid4().hex[:12]}"
        add = body_for(ses1, "add Insulated Water Bottle 750ml", rid1, cart1)
        first = await post_stream(ann, pk, add)
        check("the add completed (message + done)", first.types()[-2:] == ["message", "done"], first.types())
        check("the cart holds one", await qty(ann, cart1) == 1)
        turns_before = await transcript(ann, ses1)
        kill(eng)
        eng = spawn_engine()
        replay = await post_stream(ann, pk, add)
        check("after the restart the same request id answers from the record: the same reply", replay.status == 200 and replay.types()[-2:] == ["message", "done"]
              and replay.of("message")[0]["data"]["reply"] == first.of("message")[0]["data"]["reply"], (replay.status, replay.types()))
        check("the cart still holds one", await qty(ann, cart1) == 1)
        check("the transcript has the same number of turns", len(await transcript(ann, ses1)) == len(turns_before), (len(turns_before), len(await transcript(ann, ses1))))
        resumed = await post_stream(ann, pk, add, last_event_id=first.events[1]["id"])
        check("a resume with Last-Event-ID continues from the record (later ids only)", resumed.status == 200 and all(e["id"] > first.events[1]["id"] for e in resumed.events) and resumed.types()[-1] == "done", (resumed.status, resumed.types()))

        print("2. Isolation and body binding survive the restart")
        bob = httpx.AsyncClient()  # a different visitor
        stranger = await post_stream(bob, pk, add)
        check("another visitor naming the same session and request id: 404, no events (no oracle)", stranger.status == 404 and not stranger.events, (stranger.status, stranger.types()))
        other_msg = await post_stream(ann, pk, body_for(ses1, "add Insulated Water Bottle 750ml please", rid1, cart1))
        check("the same request id with a different message: 409, nothing run", other_msg.status == 409 and not other_msg.events, (other_msg.status, other_msg.types()))
        check("and the cart is unchanged", await qty(ann, cart1) == 1)

        print("3. A turn that was RUNNING when the engine was killed is refused as interrupted, never run again")
        ses2 = f"sess_{uuid.uuid4().hex[:10]}"
        cart2 = await new_cart(ann)
        await script(reset=True, delay=0.7, mode="normal", claims=[claim(context_action="push")])
        d1 = await post_stream(ann, pk, body_for(ses2, "show me running shoes", f"req_{uuid.uuid4().hex[:12]}", cart2))
        check("setup: a discovery turn shows products", d1.types()[-2:] == ["message", "done"], d1.types())
        await script(claims=[claim(capability="compare", context_action="continue", reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1, 2])])
        rid2 = f"req_{uuid.uuid4().hex[:12]}"
        slow_body = body_for(ses2, "compare the first two", rid2, cart2)
        slow = asyncio.create_task(post_stream(ann, pk, slow_body))
        t0 = time.time()
        while time.time() - t0 < 30:  # wait until the provider is mid-stream, so the turn is genuinely running
            async with httpx.AsyncClient() as pc:
                st = (await pc.get(f"{PROVIDER}/__state")).json()
            if st["stream_started_at"] and not st["stream_finished_at"]:
                break
            await asyncio.sleep(0.2)
        check("the provider is mid-stream: the turn is running", bool(st["stream_started_at"]) and not st["stream_finished_at"], st)
        turns_mid = await transcript(ann, ses2)
        kill(eng)
        part = await slow
        check("the client's stream died with the engine, without a final message", "message" not in part.types(), part.types())
        eng = spawn_engine()
        again = await post_stream(ann, pk, slow_body)
        check("the same request id after the restart: 409 TURN_INTERRUPTED, no events, not run again", again.status == 409 and not again.events, (again.status, again.types()))
        async with httpx.AsyncClient() as pc:
            st2 = (await pc.get(f"{PROVIDER}/__state")).json()
        check("the provider saw no second synthesis request for it", st2["stream_requests"] == st["stream_requests"], (st["stream_requests"], st2["stream_requests"]))
        check("the transcript gained nothing from the retry", len(await transcript(ann, ses2)) == len(turns_mid), (len(turns_mid), len(await transcript(ann, ses2))))
        await script(delay=0.01, claims=[claim(capability="compare", context_action="continue", reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1, 2])])
        fresh_id = await post_stream(ann, pk, body_for(ses2, "compare the first two", f"req_{uuid.uuid4().hex[:12]}", cart2))
        check("sending it again as a NEW request id works", fresh_id.status == 200 and fresh_id.types()[-2:] == ["message", "done"], (fresh_id.status, fresh_id.types()))

        print("4. Two simultaneous posts of one request id run the turn once")
        ses3 = f"sess_{uuid.uuid4().hex[:10]}"
        cart3 = await new_cart(ann)
        rid3 = f"req_{uuid.uuid4().hex[:12]}"
        add3 = body_for(ses3, "add Insulated Water Bottle 750ml", rid3, cart3)
        a, b = await asyncio.gather(post_stream(ann, pk, add3), post_stream(ann, pk, add3))
        check("both callers get the final message", a.types()[-2:] == ["message", "done"] and b.types()[-2:] == ["message", "done"], (a.types(), b.types()))
        check("the cart changed exactly once", await qty(ann, cart3) == 1, await qty(ann, cart3))
        base_ses = f"sess_{uuid.uuid4().hex[:10]}"
        base_cart = await new_cart(ann)
        await post_stream(ann, pk, body_for(base_ses, "add Insulated Water Bottle 750ml", f"req_{uuid.uuid4().hex[:12]}", base_cart))
        check("the transcript is exactly what ONE run writes (a single-post baseline), not doubled",
              len(await transcript(ann, ses3)) == len(await transcript(ann, base_ses)), (len(await transcript(ann, ses3)), len(await transcript(ann, base_ses))))
        await ann.aclose()
        await bob.aclose()
    finally:
        for proc in (eng, provider):
            try:
                proc.kill()
                proc.wait(timeout=20)
            except Exception:  # noqa: BLE001
                pass


if __name__ == "__main__":
    asyncio.run(main())
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)
