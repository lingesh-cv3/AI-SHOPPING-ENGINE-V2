"""The draft gate (`TurnStreamSink`) and the run registry, in isolation (model-free; no network, no server, no merchants).

The sink decides what model-written text may reach a shopper while the model is still writing. Asserted exactly: nothing is released before the engine's own citation check;
an ungrounded citation drops the draft; a null citation releases; only whole words go out; a cart/order/payment claim emits ONE reset and stops the draft; a failed model call
discards a shown draft; drafts can be switched off; only the whitelisted stages and tools are ever reported; and a discovery-style call that never reaches the sink emits
nothing. The registry: atomic acquire, owner isolation, body-hash mismatch, per-session single flight, caps (never evicting a live run), TTL expiry, event cap.

    python tests/shopper_v2/test_turn_stream_sink.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_turn_stream_sink.db")
for _suffix in ("", "-journal", "-wal", "-shm"):  # the registry now keeps a durable record: start every run from nothing
    if os.path.exists(_DB + _suffix):
        os.remove(_DB + _suffix)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_turn_stream_sink.db"
os.environ["OPENAI_API_KEY"] = ""
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

from fastapi import HTTPException  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {str(got)[:240]}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


ITEMS = [{"product_id": "P1", "title": "Trailblazer Running Shoe", "description": "A cushioned trail shoe with a rock plate"},
         {"product_id": "P2", "title": "Starter Road Shoe", "description": "A light, cheap road shoe"}]


def make_sink(draft=True):
    from engine.shopper_v2.streaming import TurnStreamSink

    events = []
    return TurnStreamSink(lambda t, d: events.append((t, d)), draft_enabled=draft), events


def texts(events):
    return "".join(d["text"] for t, d in events if t == "delta")


def kinds(events):
    return [t for t, _ in events]


async def feed(sink, segment, pieces):
    for p in pieces:
        await sink.delta(segment, p)


async def checks():
    print("1. Nothing is released before the engine's own citation check")
    sink, ev = make_sink()
    seg = sink.begin("cited_reason_check", ITEMS)
    await feed(sink, seg, ["The Trailblazer is ", "more cushioned. "])
    check("text written before the citation is known is held back", not ev, ev)
    await sink.field(seg, "cited_reason", None)
    check("a null citation releases the held text in ONE piece", kinds(ev) == ["delta"] and texts(ev) == "The Trailblazer is more cushioned. ", ev)
    await feed(sink, seg, ["The Starter Road ", "is lighter "])
    check("after release text flows as it arrives", texts(ev).endswith("The Starter Road is lighter ") and len(ev) >= 2, ev)

    print("\n2. A citation that is real text in the products' own wording releases; an invented one drops the draft")
    sink, ev = make_sink()
    seg = sink.begin("cited_reason_check", ITEMS)
    await sink.field(seg, "cited_reason", "cushioned trail shoe")
    await feed(sink, seg, ["A cushioned pick for trails. "])
    check("a grounded citation: text streams", texts(ev) == "A cushioned pick for trails. ", ev)
    sink, ev = make_sink()
    seg = sink.begin("cited_reason_check", ITEMS)
    await feed(sink, seg, ["Because you love marathon racing this one wins. "])
    await sink.field(seg, "cited_reason", "love marathon racing")
    await feed(sink, seg, ["More words. "])
    await sink.end(seg, True)
    check("an ungrounded citation: nothing was ever shown and no reset was needed", ev == [], ev)
    check("the held text was discarded, not kept for later", sink.draft_text() == "" and sink.streamed_chars == 0)

    print("\n3. Only whole words are released; the unfinished tail waits")
    sink, ev = make_sink()
    seg = sink.begin("cited_reason_check", ITEMS)
    await sink.field(seg, "cited_reason", None)
    await feed(sink, seg, ["Cushio", "ned and ligh", "t"])
    check("finished words go out, a half word never does ('Cushio'+'ned and ligh' releases 'Cushioned and ' and holds 'ligh')", texts(ev) == "Cushioned and " and "ligh" not in texts(ev), ev)
    await feed(sink, seg, [" feel. Next"])
    check("it goes out when its word is finished, and the new unfinished word is held", texts(ev) == "Cushioned and light feel. " and not texts(ev).endswith("Next"), texts(ev))
    await sink.end(seg, True)
    check("a tail left at the end is dropped (the final message carries it)", texts(ev) == "Cushioned and light feel. ")

    print("\n4. A cart / order / payment claim: ONE reset, the draft stops, and the claim is never shown")
    sink, ev = make_sink()
    seg = sink.begin("cited_reason_check", ITEMS)
    await sink.field(seg, "cited_reason", None)
    await feed(sink, seg, ["Both are good choices for different runners on the road. ", "I've added the ", "Trailblazer to your cart. "])
    check("the clean prefix was shown, then exactly one reset", kinds(ev).count("reset") == 1 and kinds(ev).count("delta") >= 1 and ev[-1][0] == "reset" and ev[-1][1]["reason"] == "held_back", ev)
    check("the claim itself never reached a delta", "added" not in texts(ev).lower() and "cart" not in texts(ev).lower(), texts(ev))
    before = len(ev)
    await feed(sink, seg, ["more text after the reset. "])
    check("nothing is forwarded after the reset", len(ev) == before, ev[before:])
    check("the shown draft is accounted as discarded", sink.draft_text() == "" and sink.streamed_chars == 0 and sink.resets == 1)
    for phrase in ("Your order is on its way. ", "It was charged to your card. ", "I removed it from your cart. ", "You have paid already. "):
        s2, e2 = make_sink()
        sg = s2.begin("cart_claim_screen_only", ITEMS)
        await feed(s2, sg, ["A calm opening sentence goes first here. ", phrase])
        check(f"claim words are stopped too: {phrase.strip()!r}", not any(w in texts(e2).lower() for w in ("order", "charged", "removed", "paid")), texts(e2))

    print("\n5. The compatibility-style call (cart-claim screen only) streams from the first word, with that screen")
    sink, ev = make_sink()
    seg = sink.begin("cart_claim_screen_only", ITEMS)
    await feed(sink, seg, ["These go together well. "])
    check("released at once, no citation needed", texts(ev) == "These go together well. ", ev)

    print("\n6. A failed model call discards a draft that was shown; a draft that was never shown needs no reset")
    sink, ev = make_sink()
    seg = sink.begin("cart_claim_screen_only", ITEMS)
    await feed(sink, seg, ["This is a perfectly fine start to a reply. "])
    await sink.end(seg, False)
    check("a shown draft is reset when the model fails", ev[-1] == ("reset", {"segment": seg, "reason": "model_failed"}), ev)
    sink, ev = make_sink()
    seg = sink.begin("cited_reason_check", ITEMS)
    await feed(sink, seg, ["held words "])
    await sink.end(seg, False)
    check("a held-back draft that fails emits nothing at all", ev == [], ev)

    print("\n7. Segments are numbered at entry, and each is independent")
    sink, ev = make_sink()
    a = sink.begin("cart_claim_screen_only", ITEMS)
    b = sink.begin("cited_reason_check", ITEMS)
    check("segments are 1, 2 in the order they began", (a, b) == (1, 2), (a, b))
    await feed(sink, b, ["Second call text here. "])  # pending: held
    await feed(sink, a, ["First call text here. "])
    check("a held segment does not block another and does not interleave", texts(ev) == "First call text here. ", ev)
    await sink.field(b, "cited_reason", None)
    check("the held segment releases under its own segment number", ev[-1] == ("delta", {"segment": 2, "text": "Second call text here. "}), ev)

    print("\n8. Drafts can be switched off: progress still works")
    sink, ev = make_sink(draft=False)
    seg = sink.begin("cart_claim_screen_only", ITEMS)
    await feed(sink, seg, ["Never shown. "])
    sink.node_started("discovery")
    check("no delta with drafts off, but the stage is still reported", texts(ev) == "" and ("status", {"stage": "searching", "label": "Looking through the catalogue"}) in ev, ev)
    os.environ["CHAT_STREAM_DRAFT_TEXT"] = "off"
    try:
        from engine.shopper_v2.streaming import TurnStreamSink, draft_text_enabled

        check("the environment switch reads off/0/false/no", not draft_text_enabled())
        os.environ["CHAT_STREAM_DRAFT_TEXT"] = "on"
        check("and on by default", draft_text_enabled())
    finally:
        os.environ.pop("CHAT_STREAM_DRAFT_TEXT", None)

    print("\n9. Progress is whitelisted: only known nodes speak, nothing carries a node name, argument or result")
    sink, ev = make_sink()
    for node in ("load_context", "understand", "apply_delta", "discovery", "respond", "some_internal_node", "capability_dispatch_router"):
        sink.node_started(node)
        sink.node_finished(node)
    stages = [d["stage"] for t, d in ev if t == "status"]
    check("unknown nodes say nothing; consecutive nodes in one stage report it once", stages == ["understanding", "searching", "writing"], stages)
    check("tool events name only catalogue/cart/orders and carry no other field", all(set(d) == {"type", "name", "state"} or set(d) == {"name", "state"} for t, d in ev if t == "tool")
          and all(d["name"] in {"catalogue", "cart", "orders"} for t, d in ev if t == "tool"), [d for t, d in ev if t == "tool"])
    check("no event field holds a node name", not any("understand" == str(v) or "apply_delta" in str(v) or "internal" in str(v) for t, d in ev for v in d.values()), ev)

    print("\n10. The announce wrapper: transparent without a sink, safe with a broken one")
    from engine.shopper_v2.streaming import TURN_EVENTS, announce

    async def node(state):
        return {"ok": state}

    wrapped = announce("discovery", node)
    check("no sink: the node's result is untouched", await wrapped(7) == {"ok": 7})
    sink, ev = make_sink()
    token = TURN_EVENTS.set(sink)
    try:
        out = await wrapped(8)
    finally:
        TURN_EVENTS.reset(token)
    check("with a sink: the result is untouched and start/finish are reported", out == {"ok": 8} and ("tool", {"name": "catalogue", "state": "started"}) in ev and ("tool", {"name": "catalogue", "state": "finished"}) in ev, ev)

    class Broken:
        def node_started(self, n):
            raise RuntimeError("sink bug")

        def node_finished(self, n):
            raise RuntimeError("sink bug")

    token = TURN_EVENTS.set(Broken())
    try:
        out = await wrapped(9)
    finally:
        TURN_EVENTS.reset(token)
    check("a sink that raises never breaks a turn", out == {"ok": 9})
    sync_fn = lambda state: {}  # noqa: E731
    check("a synchronous node is returned as it was (not wrapped)", announce("x", sync_fn) is sync_fn)

    print("\n11. The run registry")
    from engine.api import chat_stream as cs

    from engine.db import create_schema

    await create_schema()
    reg = cs.RunRegistry()
    key = ("conn_demo", "sess_1", "req_00000001")
    run, created = await reg.acquire(key, ["owner-a"], "hash-1")
    check("a new request id creates a run", created and run.active)
    same, created2 = await reg.acquire(key, ["owner-a"], "hash-1")
    check("the same request id from the same owner ATTACHES to that run (no second run)", same is run and not created2)
    for owners, h, label, status, code in ((["owner-b"], "hash-1", "another owner", 404, "SESSION_NOT_FOUND"), (["owner-a"], "hash-2", "a different body", 409, "REQUEST_ID_REUSED")):
        try:
            await reg.acquire(key, owners, h)
            got = None
        except HTTPException as exc:
            got = (exc.status_code, exc.detail["code"])
        check(f"{label}: {status} {code}", got == (status, code), got)
    try:
        await reg.acquire(("conn_demo", "sess_1", "req_00000002"), ["owner-a"], "hash-3")
        got = None
    except HTTPException as exc:
        got = (exc.status_code, exc.detail["code"], exc.detail["retryable"])
    check("a different request id while the session is still answering: 409 TURN_IN_PROGRESS (retryable)", got == (409, "TURN_IN_PROGRESS", True), got)
    await reg.finish(run)
    r2, c2 = await reg.acquire(("conn_demo", "sess_1", "req_00000002"), ["owner-a"], "hash-3")
    check("once the run finished a new request id is accepted", c2 and r2 is not run)
    r3, c3 = await reg.acquire(("conn_demo", "sess_2", "req_00000003"), ["owner-a"], "hash-4")
    check("another session is independent (single flight is per session)", c3)
    r_other, c_other = await reg.acquire(("conn_kettle", "sess_1", "req_00000001"), ["owner-a"], "hash-1")
    check("the same session id and request id under another merchant is a different run", c_other and r_other is not run)

    print("\n12. Caps never evict a live run; finished runs expire; the event cap keeps what matters")
    old = (cs.MAX_RUNS, cs.MAX_RUNS_PER_OWNER, cs.RUN_TTL_SECONDS, cs.MAX_EVENTS)
    try:
        cs.MAX_RUNS_PER_OWNER = 2
        cap = cs.RunRegistry()
        await cap.acquire(("c", "s1", "req_cap00001"), ["o"], "h")
        await cap.acquire(("c", "s2", "req_cap00002"), ["o"], "h")
        try:
            await cap.acquire(("c", "s3", "req_cap00003"), ["o"], "h")
            got = None
        except HTTPException as exc:
            got = exc.status_code
        check("over the per-owner cap: 429, and the live runs are still there", got == 429 and len(cap._runs) == 2, (got, len(cap._runs)))
        await cap.acquire(("c", "s9", "req_cap00009"), ["other-owner"], "h")
        check("one owner at the cap does not block another", len(cap._runs) == 3)
        cs.MAX_RUNS_PER_OWNER = 30
        cs.MAX_RUNS = 2
        glob = cs.RunRegistry()
        await glob.acquire(("c", "a", "req_glob0001"), ["x"], "h")
        await glob.acquire(("c", "b", "req_glob0002"), ["y"], "h")
        try:
            await glob.acquire(("c", "c", "req_glob0003"), ["z"], "h")
            got = None
        except HTTPException as exc:
            got = exc.status_code
        check("over the global cap: 429", got == 429, got)
        cs.MAX_RUNS = 500
        cs.RUN_TTL_SECONDS = 0.0
        ttl = cs.RunRegistry()
        old_run, _ = await ttl.acquire(("c", "s", "req_ttl00001"), ["o"], "h")
        await ttl.finish(old_run)
        await asyncio.sleep(0.01)
        await ttl.acquire(("c", "other", "req_ttl00002"), ["o"], "h")
        check("a finished run past its TTL is dropped on the next acquire", ("c", "s", "req_ttl00001") not in ttl._runs)
        live, _ = await ttl.acquire(("c", "live", "req_ttl00003"), ["o"], "h")
        await asyncio.sleep(0.01)
        await ttl.acquire(("c", "x", "req_ttl00004"), ["o"], "h")
        check("a LIVE run is never expired", ("c", "live", "req_ttl00003") in ttl._runs)
        cs.MAX_EVENTS = 5
        run_cap = cs.TurnRun(key=("c", "s", "r"), owner_keys=frozenset({"o"}), body_hash="h")
        for i in range(10):
            run_cap.emit("delta", {"text": str(i)})
        run_cap.emit("reset", {"segment": 1, "reason": "x"})
        run_cap.emit("message", {"reply": "final"})
        run_cap.emit("done", {})
        types = [e.type for e in run_cap.events]
        check("past the event cap deltas are dropped but reset, message and done are never lost", types.count("delta") == 5 and types[-3:] == ["reset", "message", "done"], types)
        ids = [e.id for e in run_cap.events]
        check("ids stay contiguous", ids == list(range(1, len(ids) + 1)), ids)
    finally:
        cs.MAX_RUNS, cs.MAX_RUNS_PER_OWNER, cs.RUN_TTL_SECONDS, cs.MAX_EVENTS = old

    print("\n13. A reader waiting on a run is woken by the next event and times out cleanly")
    wake = cs.TurnRun(key=("c", "s", "r"), owner_keys=frozenset({"o"}), body_hash="h")
    waiter = asyncio.create_task(wake.wait_for_more(2.0))
    await asyncio.sleep(0.05)
    wake.emit("status", {"stage": "x", "label": "y"})
    check("an emitted event wakes a waiting reader", await waiter is True)
    check("no event: the wait times out and returns False", await wake.wait_for_more(0.05) is False)
    check("the waiter list does not leak", wake._waiters == [])


if __name__ == "__main__":
    asyncio.run(checks())
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)
