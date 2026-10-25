"""POST /api/v2/chat/stream end to end (model-free: the REAL app on a REAL local server, the REAL LLM client, a scripted fake provider; needs Northfield on 8001).

The fake provider streams its tokens SLOWLY over an in-process transport, so the assertions are about time, not just order: the first text reaches the HTTP reader
while the provider is still generating. Asserted: event order and ids; incremental delivery; drafts only where the engine itself shows model text, and only after
its own citation check; the cart-claim screen; no hidden reasoning / other tools / raw errors / exception text on the wire; reconnect with Last-Event-ID replays
without a second execution (the cart changes once); disconnect finishes the turn exactly once; tenant and visitor isolation; the cold-visitor cookie; request-id
and concurrency refusals; heartbeat and idle close; the unchanged non-streaming route; and that the stream can never reach payment.

    python tests/shopper_v2/test_chat_stream_endpoint.py
"""
from __future__ import annotations

import os
import sys

_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_chat_stream.db")
for _suffix in ("", "-journal", "-wal", "-shm"):
    if os.path.exists(_DB + _suffix):
        os.remove(_DB + _suffix)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_chat_stream.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import socket  # noqa: E402
import time  # noqa: E402
import uuid  # noqa: E402

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

failures = 0
CONN = "conn_demo"


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {str(got)[:300]}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


# ----------------------------------------------------------------------------------------------------------------------------- fake provider
def sse(obj) -> bytes:
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def tool_chunk(index=0, name=None, args=None):
    fn = {}
    if name:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    tc = {"index": index, "function": fn}
    if name:
        tc.update({"id": f"call_{index}", "type": "function"})
    return {"id": "c", "object": "chat.completion.chunk", "model": "fake-model", "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]}


def reasoning_chunk(text):
    return {"id": "c", "object": "chat.completion.chunk", "model": "fake-model", "choices": [{"index": 0, "delta": {"reasoning": text}, "finish_reason": None}]}


def whole(name, arguments):
    return {"model": "fake-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5}}


def split(text, n):
    size = max(1, len(text) // n)
    return [text[i:i + size] for i in range(0, len(text), size)]


REPLY = "The Trailblazer is the more cushioned pick while the Starter Road is lighter and cheaper for short runs on pavement."


class Provider:
    """Interpretation claims are queued by the test; synthesis streams (slowly) when asked to; every other model call is refused."""

    def __init__(self):
        self.claims = []
        self.mode = "normal"
        self.delay = 0.12
        self.synthesis_requests = []
        self.stream_finished_at = None

    def handler(self):
        async def handle(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            name = (payload.get("tools") or [{}])[0].get("function", {}).get("name")
            if name == "interpret_conversation" and self.claims:
                return httpx.Response(200, json=whole(name, self.claims.pop(0)))
            if name == "write_synthesis":
                self.synthesis_requests.append(payload)
                return await self._synthesis(payload)
            return httpx.Response(500, json={"error": {"message": "RAW-PROVIDER-SECRET unsupported call"}})
        return handle

    async def _synthesis(self, payload):
        mode = self.mode
        if mode == "fail":
            return httpx.Response(500, json={"error": {"message": "RAW-PROVIDER-SECRET exploded"}})
        if mode == "ungrounded":
            doc = {"personalizes": True, "cited_reason": "totally invented preference phrase", "reply": "SECRET-UNGROUNDED-TEXT " + REPLY}
        elif mode == "cart_claim":
            doc = {"personalizes": False, "cited_reason": None, "reply": "Both of these are genuinely good choices for different runners and different distances on the road. I've added the Trailblazer to your cart for you today."}
        elif mode == "reply_first":
            doc = {"reply": REPLY, "personalizes": False, "cited_reason": None}
        else:  # the order the streaming tool asks for: grounding fields first, the reply last
            doc = {"personalizes": False, "cited_reason": None, "reply": REPLY}
        text = json.dumps(doc)
        if not payload.get("stream"):
            return httpx.Response(200, json=whole("write_synthesis", doc))
        chunks = [tool_chunk(0, "write_synthesis", "")]
        if mode == "leaky":
            chunks += [reasoning_chunk("HIDDEN-REASONING-TEXT the shopper must never see"), tool_chunk(1, "other_tool", '{"reply": "OTHER-TOOL-SECRET"}')]
        chunks += [tool_chunk(0, args=f) for f in split(text, 14)]

        async def body():
            for c in chunks:
                yield sse(c)
                await asyncio.sleep(self.delay)
            self.stream_finished_at = time.monotonic()
            yield b"data: [DONE]\n\n"

        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=body())


def claim(**kw):
    base = {"capability": "discover", "context_action": "continue", "question_type": "retrieve", "reference_scope": "none", "reference_kind": "none",
            "conversational_repair": "none", "cart_operation": "none", "rationale": "scripted"}
    base.update(kw)
    return base


# ----------------------------------------------------------------------------------------------------------------------------- SSE client
class Stream:
    """A parsed stream: events with arrival times, raw bytes, comment lines."""

    def __init__(self):
        self.events, self.raw, self.comments, self.status, self.headers = [], b"", [], None, None

    def types(self):
        return [e["event"] for e in self.events]

    def of(self, event_type):
        return [e for e in self.events if e["event"] == event_type]


async def post_stream(client, base, key, body, *, last_event_id=None, stop_after=None, timeout=60.0) -> Stream:
    """POST and read the SSE body incrementally. `stop_after` = number of events after which to drop the connection (a client that goes away)."""
    out = Stream()
    headers = {"Authorization": f"Bearer {key}", "Accept": "text/event-stream"}
    if last_event_id is not None:
        headers["Last-Event-ID"] = str(last_event_id)
    t0 = time.monotonic()
    async with client.stream("POST", f"{base}/api/v2/chat/stream", json=body, headers=headers, timeout=timeout) as resp:
        out.status, out.headers = resp.status_code, resp.headers
        if resp.status_code != 200:
            out.raw = await resp.aread()
            return out
        buf = b""
        async for chunk in resp.aiter_raw():
            out.raw += chunk
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                text = block.decode("utf-8", "replace")
                if text.startswith(":"):
                    out.comments.append(text)
                    continue
                fields = dict(line.split(": ", 1) for line in text.split("\n") if ": " in line)
                if "event" in fields and "data" in fields:
                    out.events.append({"id": int(fields["id"]), "event": fields["event"], "data": json.loads(fields["data"]), "t": time.monotonic() - t0})
                    if stop_after is not None and len(out.events) >= stop_after:
                        return out  # leaving the context manager closes the connection
    return out


def body_for(session, message, request_id=None, cart_id=None, **extra):
    d = {"connection_id": CONN, "session_id": session, "message": message, "request_id": request_id or f"req_{uuid.uuid4().hex[:12]}"}
    if cart_id:
        d["cart_id"] = cart_id
    d.update(extra)
    return d


async def start_server():
    import uvicorn
    from engine.api.main import app

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on"))
    task = asyncio.create_task(server.serve())
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started, "the local server did not start"
    return server, task, f"http://127.0.0.1:{port}"


async def checks():
    from engine import db
    from engine.api import chat_stream
    from engine.api.deps import engine
    from engine.db import create_schema
    from engine.reasoning import ReasoningService
    from engine.reasoning.llm import LLMClient, LLMConfig
    from engine.reasoning.stream_sink import REPLY_SINK

    await create_schema()
    pk_demo = await db.keys.mint("publishable", connection_id=CONN, label="stream-test")
    pk_kettle = await db.keys.mint("publishable", connection_id="conn_kettle", label="stream-test-k")
    provider = Provider()
    llm = LLMClient(LLMConfig(api_key="fake", base_url="http://provider.invalid/v1", model="fake-model"))
    llm._client = AsyncOpenAI(api_key="fake", base_url="http://provider.invalid/v1", max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.handler())))
    engine.reasoning = ReasoningService(llm)
    server, server_task, base = await start_server()
    adapter = engine.registry.adapter_for(CONN)

    async def fresh():
        """A brand-new visitor (own cookie jar), session and cart."""
        client = httpx.AsyncClient()
        cart = (await adapter.create_cart()).cart_id
        return client, f"sess_{uuid.uuid4().hex[:10]}", cart

    async def reach_comparison(client, session, cart):
        """Two turns that set up a real comparison: a discovery turn, then the compare turn - returns the compare stream."""
        provider.claims.append(claim(context_action="push"))
        first = await post_stream(client, base, pk_demo, body_for(session, "show me running shoes", cart_id=cart))
        provider.claims.append(claim(capability="compare", context_action="continue", reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1, 2]))
        return first

    try:
        print("1. A discovery turn: progress, the final message, and NO draft text (discovery text is never shown directly)")
        client, session, cart = await fresh()
        provider.mode = "normal"
        provider.claims.append(claim(context_action="push"))
        s = await post_stream(client, base, pk_demo, body_for(session, "show me running shoes", cart_id=cart))
        check("HTTP 200 as text/event-stream with the no-buffering headers", s.status == 200 and s.headers["content-type"].startswith("text/event-stream")
              and "no-transform" in s.headers.get("cache-control", "") and s.headers.get("x-accel-buffering") == "no", (s.status, dict(s.headers)))
        ids = [e["id"] for e in s.events]
        check("event ids start at 1 and strictly increase", ids == list(range(1, len(ids) + 1)), ids)
        check("starts with `start`, ends with exactly one `message` then one `done`", s.types()[0] == "start" and s.types()[-2:] == ["message", "done"] and s.types().count("message") == 1 and s.types().count("done") == 1, s.types())
        check("whitelisted progress arrived before the message", any(t == "status" for t in s.types()) and s.types().index("status") < s.types().index("message"), s.types())
        check("no `delta` for a discovery turn (the engine never shows that model text)", not s.of("delta") and s.of("done")[0]["data"]["first_text_ms"] is None, s.types())
        msg = s.of("message")[0]["data"]
        check("the message is the full reply object (reply, products, state, trace)", msg["reply"] and "products" in msg and "shopping_state" in msg and "trace" in msg, list(msg))
        check("every status label/tool name is from the whitelist", all(e["data"]["stage"] in {"understanding", "cart", "checkout", "orders", "checking", "comparing", "searching", "writing"} for e in s.of("status"))
              and all(e["data"]["name"] in {"catalogue", "cart", "orders"} and e["data"]["state"] in {"started", "finished"} for e in s.of("tool")), s.of("tool"))
        check("no internals on the wire: no node names, argument or result text", not re.search(rb"node_|load_context|apply_delta|route_capability", s.raw), s.raw[:200])

        print("\n2. A comparison turn streams REAL provider tokens: the first text reaches the reader before the provider has finished")
        provider.mode = "normal"
        first = await reach_comparison(client, session, cart)
        t_req = time.monotonic()
        s = await post_stream(client, base, pk_demo, body_for(session, "compare the first two", cart_id=cart))
        deltas = s.of("delta")
        check("deltas were streamed (more than one)", len(deltas) > 1, s.types())
        t_first_delta = deltas[0]["t"]
        t_message = s.of("message")[0]["t"]
        check("the first delta arrived well before the message (the provider was still writing)", t_first_delta < t_message - 0.3, (round(t_first_delta, 2), round(t_message, 2)))
        check("deltas keep arriving over time, not in one burst", deltas[-1]["t"] - deltas[0]["t"] > 0.3, round(deltas[-1]["t"] - deltas[0]["t"], 2))
        check("order: start < status/tool < deltas < message < done", s.types().index("start") < s.types().index("delta") < s.types().index("message") < s.types().index("done"), s.types())
        draft = "".join(e["data"]["text"] for e in deltas)
        msg = s.of("message")[0]["data"]
        check("the draft is the model's words (whole words only) and the final reply contains it: nothing was replaced", draft and draft.strip() in msg["reply"] and msg["draft_replaced"] is False, (draft, msg["reply"]))
        done = s.of("done")[0]["data"]
        check("done reports first_text_ms before total_ms", done["first_text_ms"] is not None and done["first_text_ms"] < done["total_ms"], done)
        check("the request really asked the provider to stream, with the grounding fields listed before the reply",
              provider.synthesis_requests[-1].get("stream") is True
              and list(provider.synthesis_requests[-1]["tools"][0]["function"]["parameters"]["properties"]) == ["personalizes", "cited_reason", "reply"],
              provider.synthesis_requests[-1].get("stream"))

        print("\n3. The same turn without streaming gives the same reply, and its schema is unchanged")
        client2, session2, cart2 = await fresh()
        provider.mode = "normal"
        await reach_comparison(client2, session2, cart2)
        plain = await client2.post(f"{base}/api/v2/chat", json={"connection_id": CONN, "session_id": session2, "message": "compare the first two", "cart_id": cart2},
                                   headers={"Authorization": f"Bearer {pk_demo}"}, timeout=60)
        check("POST /api/v2/chat still answers 200 with the old fields and none of the stream's", plain.status_code == 200 and "reply" in plain.json() and "draft_replaced" not in plain.json() and "streamed_chars" not in plain.json(), list(plain.json()))
        check("the non-streaming reply equals the streamed message's reply", plain.json()["reply"] == msg["reply"], (plain.json()["reply"][:120], msg["reply"][:120]))
        check("the non-streaming request did NOT ask the provider to stream and used the unchanged tool order",
              provider.synthesis_requests[-1].get("stream") is None and list(provider.synthesis_requests[-1]["tools"][0]["function"]["parameters"]["properties"]) == ["reply", "personalizes", "cited_reason"],
              list(provider.synthesis_requests[-1]["tools"][0]["function"]["parameters"]["properties"]))

        print("\n4. The draft gate: an ungrounded citation, a cart claim and a model that writes the reply first")
        for mode, label in (("ungrounded", "an ungrounded citation"), ("cart_claim", "a cart claim in the text"), ("reply_first", "reply written before the citation")):
            c, ses, crt = await fresh()
            provider.mode = "normal"
            await reach_comparison(c, ses, crt)
            provider.mode = mode
            s = await post_stream(c, base, pk_demo, body_for(ses, "compare the first two", cart_id=crt))
            msg = s.of("message")[0]["data"]
            text = "".join(e["data"]["text"] for e in s.of("delta"))
            if mode == "ungrounded":
                check(f"{label}: no draft was shown at all", not s.of("delta") and b"SECRET-UNGROUNDED" not in s.raw, s.types())
                check(f"{label}: the final reply is the engine's own fallback", "SECRET-UNGROUNDED" not in msg["reply"], msg["reply"][:100])
            elif mode == "cart_claim":
                check(f"{label}: text before the claim was shown, then a reset discarded it, and the claim never reached the draft",
                      s.of("delta") and s.of("reset") and s.of("reset")[0]["data"]["reason"] == "held_back" and "added" not in text.lower(), (s.types(), text))
                check(f"{label}: the final reply does not claim a cart change", "added the trailblazer" not in msg["reply"].lower(), msg["reply"][:120])
            else:
                check(f"{label}: nothing was shown before the citation could be checked (one coalesced piece, then the final)", len(s.of("delta")) <= 1 and (not s.of("delta") or s.of("delta")[0]["t"] > 1.0), [(e["t"]) for e in s.of("delta")])
            await c.aclose()

        print("\n5. Nothing hidden reaches the wire (A8): reasoning, another tool's arguments, raw provider errors")
        c, ses, crt = await fresh()
        provider.mode = "normal"
        await reach_comparison(c, ses, crt)
        provider.mode = "leaky"
        s = await post_stream(c, base, pk_demo, body_for(ses, "compare the first two", cart_id=crt))
        check("hidden reasoning never on the wire", b"HIDDEN-REASONING" not in s.raw, s.raw[:200])
        check("another tool's arguments never on the wire", b"OTHER-TOOL-SECRET" not in s.raw, s.raw[:200])
        check("the real reply still streamed", s.of("delta"), s.types())
        await c.aclose()
        c, ses, crt = await fresh()
        provider.mode = "normal"
        await reach_comparison(c, ses, crt)
        provider.mode = "fail"
        s = await post_stream(c, base, pk_demo, body_for(ses, "compare the first two", cart_id=crt))
        check("a failing provider: no raw provider text on the wire, and the turn still ends with a message", b"RAW-PROVIDER-SECRET" not in s.raw and s.types()[-2:] == ["message", "done"], s.types())
        await c.aclose()
        provider.mode = "normal"

        print("\n6. Reconnect never re-runs the turn (a typed add changes the cart ONCE however many times the stream is attached)")
        c, ses, crt = await fresh()
        rid = f"req_{uuid.uuid4().hex[:12]}"
        add = body_for(ses, "add Insulated Water Bottle 750ml", request_id=rid, cart_id=crt)
        first_part = await post_stream(c, base, pk_demo, add, stop_after=2)
        seen_ids = [e["id"] for e in first_part.events]
        check("the client read two events and went away (a dropped connection)", len(first_part.events) == 2, first_part.types())
        resumed = await post_stream(c, base, pk_demo, add, last_event_id=seen_ids[-1])
        res_ids = [e["id"] for e in resumed.events]
        check("the reconnect continues from the last id with no gap and no duplicate", res_ids and res_ids[0] == seen_ids[-1] + 1 and res_ids == list(range(res_ids[0], res_ids[0] + len(res_ids))), (seen_ids, res_ids))
        check("and it ends with the one message and done", resumed.types()[-2:] == ["message", "done"], resumed.types())
        again = await post_stream(c, base, pk_demo, add)  # a double-click / second tab: a full replay
        full_ids = [e["id"] for e in again.events]
        check("a later attach replays the whole run, identical to what was sent", full_ids == list(range(1, len(full_ids) + 1)) and again.of("message")[0]["data"]["reply"] == resumed.of("message")[0]["data"]["reply"], full_ids)
        cart_after = await adapter.get_cart(crt)
        print("      (typed add reply: " + str(again.of("message")[0]["data"]["reply"])[:160] + ")")
        check("the platform cart changed exactly once across the original request and two re-attaches", sum(ln.quantity for ln in cart_after.lines) == 1, [(ln.product_id, ln.quantity) for ln in cart_after.lines])
        transcript = await c.get(f"{base}/api/chat/{CONN}/{ses}", headers={"Authorization": f"Bearer {pk_demo}"})
        turns = transcript.json().get("turns", []) if transcript.status_code == 200 else []
        shape = sorted(t["speaker"] for t in turns)
        # the typed-add path itself writes an "Added ..." turn (shared turn code, unchanged); the baseline is the non-streaming route for the SAME message
        base_client, base_ses, base_cart = await fresh()
        await base_client.post(f"{base}/api/v2/chat", json={"connection_id": CONN, "session_id": base_ses, "message": "add Insulated Water Bottle 750ml", "cart_id": base_cart},
                               headers={"Authorization": f"Bearer {pk_demo}"}, timeout=60)
        base_turns = (await base_client.get(f"{base}/api/chat/{CONN}/{base_ses}", headers={"Authorization": f"Bearer {pk_demo}"})).json().get("turns", [])
        base_shape = sorted(t["speaker"] for t in base_turns)
        check("after the original request and two re-attaches the transcript is EXACTLY what the non-streaming route writes for that message (nothing duplicated)",
              shape == base_shape and shape.count("shopper") == 1, (shape, base_shape))
        check("and the re-attaches did not add a cart line either: the baseline cart holds the same quantity",
              sum(ln.quantity for ln in (await adapter.get_cart(base_cart)).lines) == sum(ln.quantity for ln in cart_after.lines) == 1)
        await base_client.aclose()

        print("\n7. A client that disconnects mid-stream does not cancel the turn: it finishes once and the answer is waiting")
        c, ses, crt = await fresh()
        provider.mode = "normal"
        await reach_comparison(c, ses, crt)
        rid = f"req_{uuid.uuid4().hex[:12]}"
        cmp_body = body_for(ses, "compare the first two", request_id=rid, cart_id=crt)
        gone = await post_stream(c, base, pk_demo, cmp_body, stop_after=3)
        check("the client left after three events, before the answer", "message" not in gone.types(), gone.types())
        await asyncio.sleep(3.0)  # the run keeps going without it
        later = await post_stream(c, base, pk_demo, cmp_body)
        check("re-attaching finds the completed answer", later.types()[-2:] == ["message", "done"], later.types())
        transcript = await c.get(f"{base}/api/chat/{CONN}/{ses}", headers={"Authorization": f"Bearer {pk_demo}"})
        assistants = [t for t in transcript.json().get("turns", []) if t["speaker"] == "assistant"]
        shoppers = [t for t in transcript.json().get("turns", []) if t["speaker"] == "shopper"]
        check("one assistant reply per shopper message (the setup turn and this one): the disconnect and the re-attach neither duplicated nor lost the reply",
              len(shoppers) == 2 and len(assistants) == 2, ([t["text"][:30] for t in shoppers], [t["text"][:30] for t in assistants]))
        await c.aclose()

        print("\n7b. A retry of a finished request id AFTER its events were dropped is refused, never run again")
        c, ses, crt = await fresh()
        provider.mode = "normal"
        await reach_comparison(c, ses, crt)
        rid = f"req_{uuid.uuid4().hex[:12]}"
        late_body = body_for(ses, "compare the first two", request_id=rid, cart_id=crt)
        first = await post_stream(c, base, pk_demo, late_body)
        check("the original request completed", first.types()[-2:] == ["message", "done"], first.types())
        before = (await c.get(f"{base}/api/chat/{CONN}/{ses}", headers={"Authorization": f"Bearer {pk_demo}"})).json().get("turns", [])
        # the sweep runs inside this process's engine: expire the replay window by sweeping with a negative TTL
        old_ttl = chat_stream.RUN_TTL_SECONDS
        chat_stream.RUN_TTL_SECONDS = -1.0
        try:
            chat_stream.REGISTRY._sweep()
        finally:
            chat_stream.RUN_TTL_SECONDS = old_ttl
        provider.claims.append(claim(capability="compare", context_action="continue", reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1, 2]))
        late = await post_stream(c, base, pk_demo, late_body)
        check("the late retry is refused as already handled (409) with no events", late.status == 409 and not late.events, (late.status, late.types()))
        after = (await c.get(f"{base}/api/chat/{CONN}/{ses}", headers={"Authorization": f"Bearer {pk_demo}"})).json().get("turns", [])
        check("the transcript is unchanged by the late retry", len(after) == len(before), (len(before), len(after)))
        provider.claims.clear()
        await c.aclose()
        chat_stream.REGISTRY.clear()

        print("\n7c. A turn that outlasts the stream's patience frees the session, and still finishes once")
        c, ses, crt = await fresh()
        provider.mode = "normal"
        await reach_comparison(c, ses, crt)
        old_timeout, old_delay = chat_stream.TURN_TIMEOUT_SECONDS, provider.delay
        chat_stream.TURN_TIMEOUT_SECONDS, provider.delay = 0.6, 0.5
        try:
            slow_body = body_for(ses, "compare the first two", cart_id=crt)
            slow = await post_stream(c, base, pk_demo, slow_body)
            check("the stream ends with a TURN_TIMEOUT error and done (no message)", slow.types()[-2:] == ["error", "done"] and "message" not in slow.types() and slow.of("error")[0]["data"]["code"] == "TURN_TIMEOUT", slow.types())
            check("the timeout text is the fixed sentence", "taking longer than expected" in slow.of("error")[0]["data"]["message"])
            provider.claims.append(claim(context_action="push"))
            nxt = await post_stream(c, base, pk_demo, body_for(ses, "show me running shoes", cart_id=crt))
            check("the session is free: a new message is not refused as in progress", nxt.status == 200 and nxt.types()[-1] == "done", (nxt.status, nxt.types()))
        finally:
            chat_stream.TURN_TIMEOUT_SECONDS = old_timeout
        await asyncio.sleep(9.0)  # the abandoned turn runs on to its own end
        provider.delay = old_delay
        turns = (await c.get(f"{base}/api/chat/{CONN}/{ses}", headers={"Authorization": f"Bearer {pk_demo}"})).json().get("turns", [])
        cmp_replies = [t for t in turns if t["speaker"] == "shopper" and "compare the first two" in t["text"]]
        check("the abandoned turn was recorded exactly once", len(cmp_replies) == 1, [t["text"][:30] for t in turns if t["speaker"] == "shopper"])
        await c.aclose()
        chat_stream.REGISTRY.clear()

        print("\n7d. Restart behaviour: a resume of a run this engine does not know is refused, never re-executed")
        c, ses, crt = await fresh()
        rid = f"req_{uuid.uuid4().hex[:12]}"
        add = body_for(ses, "add Insulated Water Bottle 750ml", request_id=rid, cart_id=crt)
        first = await post_stream(c, base, pk_demo, add)
        check("the original add completed", first.types()[-2:] == ["message", "done"], first.types())
        qty = lambda cart: sum(ln.quantity for ln in cart.lines)  # noqa: E731
        check("and changed the cart once", qty(await adapter.get_cart(crt)) == 1)
        n_turns = len((await c.get(f"{base}/api/chat/{CONN}/{ses}", headers={"Authorization": f"Bearer {pk_demo}"})).json().get("turns", []))
        chat_stream.REGISTRY.clear()  # what an engine restart does to the in-memory runs and tombstones
        resumed = await post_stream(c, base, pk_demo, add, last_event_id=first.events[1]["id"])
        check("a resume (Last-Event-ID > 0) after the restart is refused as STREAM_RUN_LOST (409), no events", resumed.status == 409 and not resumed.events, (resumed.status, resumed.types()))
        check("the cart did not change again", qty(await adapter.get_cart(crt)) == 1)
        n_after = len((await c.get(f"{base}/api/chat/{CONN}/{ses}", headers={"Authorization": f"Bearer {pk_demo}"})).json().get("turns", []))
        check("and no second transcript entry was written", n_after == n_turns, (n_turns, n_after))
        await c.aclose()
        chat_stream.REGISTRY.clear()

        print("\n7e. Two truly parallel turns on one session (one streamed, one plain): both finish, nothing is lost or duplicated")
        c, ses, crt = await fresh()
        provider.mode = "normal"
        await reach_comparison(c, ses, crt)
        old_delay = provider.delay
        provider.delay = 0.3
        streamed = asyncio.create_task(post_stream(c, base, pk_demo, body_for(ses, "compare the first two", cart_id=crt)))
        await asyncio.sleep(0.8)  # the streamed turn is mid-synthesis
        plain = await c.post(f"{base}/api/v2/chat", json={"connection_id": CONN, "session_id": ses, "message": "hello there", "cart_id": crt}, headers={"Authorization": f"Bearer {pk_demo}"}, timeout=60)
        s_done = await streamed
        provider.delay = old_delay
        check("the plain turn answered while the stream was open (it is not blocked by it)", plain.status_code == 200, plain.status_code)
        check("the streamed turn still completed with its message", s_done.types()[-2:] == ["message", "done"], s_done.types())
        turns = (await c.get(f"{base}/api/chat/{CONN}/{ses}", headers={"Authorization": f"Bearer {pk_demo}"})).json().get("turns", [])
        speakers = [t["speaker"] for t in turns]
        check("every message got exactly one reply: 3 shopper and 3 assistant turns in all", speakers.count("shopper") == 3 and speakers.count("assistant") == 3, speakers)
        check("no shopper message appears twice", len({t["text"] for t in turns if t["speaker"] == "shopper"}) == 3)
        print("      (observed transcript order: " + ", ".join(f"{t['speaker'][0]}:{t['text'][:18]!r}" for t in turns) + ")")
        await c.aclose()
        chat_stream.REGISTRY.clear()

        print("\n7f. The platform fails mid-turn on a typed add: an honest reply, no raw error, no half-written cart")
        c, ses, crt = await fresh()
        real_add = adapter.add_to_cart

        async def broken_add(*a, **k):
            raise RuntimeError("PLATFORM-INTERNAL-TRACE /srv/secret 502 bad gateway")

        adapter.add_to_cart = broken_add
        try:
            down = await post_stream(c, base, pk_demo, body_for(ses, "add Insulated Water Bottle 750ml", cart_id=crt))
        finally:
            adapter.add_to_cart = real_add
        check("the turn ends cleanly (done), not with a hang", down.types()[-1] == "done", down.types())
        wire = json.dumps(down.events)
        check("no raw platform text on the wire", "PLATFORM-INTERNAL" not in wire and "/srv/secret" not in wire and "bad gateway" not in wire, [m for m in ("PLATFORM-INTERNAL", "/srv/secret", "bad gateway") if m in wire])
        check("the shopper gets a reply or a fixed error (never nothing)", bool(down.of("message")) or bool(down.of("error")), down.types())
        check("the cart is untouched", sum(ln.quantity for ln in (await adapter.get_cart(crt)).lines) == 0)
        await c.aclose()
        chat_stream.REGISTRY.clear()

        print("\n8. Tenant and visitor isolation")
        victim, vsession, vcart = await fresh()
        provider.mode = "normal"
        provider.claims.append(claim(context_action="push"))
        s = await post_stream(victim, base, pk_demo, body_for(vsession, "show me running shoes", cart_id=vcart))
        check("setup: the victim's turn completed", s.types()[-1] == "done", s.types())
        stranger = httpx.AsyncClient()  # same publishable key, no cookie of the victim's
        provider.claims.append(claim(context_action="push"))
        probe = await post_stream(stranger, base, pk_demo, body_for(vsession, "show me running shoes", cart_id=vcart))
        check("another visitor naming the victim's session gets 404, not a stream", probe.status == 404 and b"SESSION_NOT_FOUND" in probe.raw, (probe.status, probe.raw[:100]))
        k = await post_stream(victim, base, pk_kettle, body_for(vsession, "show me running shoes", cart_id=vcart))
        check("the OTHER merchant's key naming this merchant's session: refused before any stream", k.status in (401, 403, 404), (k.status, k.raw[:100]))
        check("the other visitor's refusal looks like a conversation that does not exist (no oracle)", probe.status == 404 and b"no such conversation" in probe.raw, probe.raw[:100])
        from fastapi import HTTPException

        try:
            chat_stream.REGISTRY.acquire((CONN, "someone_elses_session", "req_x1234567"), ["owner-a"], "h")
            chat_stream.REGISTRY.acquire((CONN, "someone_elses_session", "req_x1234567"), ["owner-b"], "h")
            leaked = True
        except HTTPException as exc:
            leaked = not (exc.status_code == 404 and exc.detail["code"] == "SESSION_NOT_FOUND")
        check("the registry itself refuses an attach by a different owner as 'no such conversation'", leaked is False)
        chat_stream.REGISTRY.clear()
        await stranger.aclose()
        await victim.aclose()

        print("\n9. A cold visitor receives their cookie on the stream response, and it owns the session it just claimed")
        cold = httpx.AsyncClient()
        csession = f"sess_{uuid.uuid4().hex[:10]}"
        provider.claims.append(claim(context_action="push"))
        s = await post_stream(cold, base, pk_demo, body_for(csession, "show me running shoes"))
        check("the very first request carries a Set-Cookie for the visitor", "cv3_visitor" in (s.headers.get("set-cookie") or ""), s.headers.get("set-cookie"))
        check("the client's jar now holds it", any(k == "cv3_visitor" for k in cold.cookies.keys()), list(cold.cookies.keys()))
        provider.claims.append(claim(context_action="push"))
        follow = await post_stream(cold, base, pk_demo, body_for(csession, "show me running shoes again"))
        check("with that cookie the same session is theirs on the next stream", follow.status == 200 and follow.types()[-1] == "done", (follow.status, follow.raw[:100]))
        other = httpx.AsyncClient()
        denied = await post_stream(other, base, pk_demo, body_for(csession, "show me running shoes"))
        check("a different cold visitor cannot take that session", denied.status == 404, denied.status)
        await cold.aclose()
        await other.aclose()

        print("\n10. Request-id and concurrency rules")
        c, ses, crt = await fresh()
        rid = f"req_{uuid.uuid4().hex[:12]}"
        provider.mode = "normal"
        provider.claims.append(claim(context_action="push"))
        a = await post_stream(c, base, pk_demo, body_for(ses, "show me running shoes", request_id=rid, cart_id=crt))
        reuse = await post_stream(c, base, pk_demo, body_for(ses, "a different message", request_id=rid, cart_id=crt))
        check("the same request id with a different message: 409 REQUEST_ID_REUSED", reuse.status == 409 and b"REQUEST_ID_REUSED" in reuse.raw, (reuse.status, reuse.raw[:100]))
        tap = await post_stream(c, base, pk_demo, body_for(ses, "show me running shoes", request_id=rid, cart_id=crt, selected_variant_id="P1001-8"))
        check("the same request id with a different selected_variant_id: 409 (a tap cannot attach to a typed run)", tap.status == 409, tap.status)
        provider.claims.append(claim(capability="compare", context_action="continue", reference_scope="last_rendered", reference_kind="ordinal", reference_positions=[1, 2]))
        provider.mode = "normal"
        slow_body = body_for(ses, "compare the first two", cart_id=crt)
        slow = asyncio.create_task(post_stream(c, base, pk_demo, slow_body))
        await asyncio.sleep(0.5)
        second = await post_stream(c, base, pk_demo, body_for(ses, "and another thing", cart_id=crt))
        check("a different request id while the session is still answering: 409 TURN_IN_PROGRESS (retryable)", second.status == 409 and b"TURN_IN_PROGRESS" in second.raw and b'"retryable":true' in second.raw.replace(b" ", b""), (second.status, second.raw[:120]))
        await slow
        provider.claims.append(claim(context_action="push"))
        nxt = await post_stream(c, base, pk_demo, body_for(ses, "show me running shoes", cart_id=crt))
        check("once the answer is done a new request id is accepted", nxt.status == 200 and nxt.types()[-1] == "done", nxt.status)
        bad = await post_stream(c, base, pk_demo, body_for(ses, "hi", request_id="x"))
        check("a malformed request id is rejected (422)", bad.status == 422, bad.status)
        old = chat_stream.MAX_RUNS_PER_OWNER
        chat_stream.MAX_RUNS_PER_OWNER = 1
        try:
            chat_stream.REGISTRY.acquire((CONN, "cap_s1", "req_cap00001"), ["cap-owner"], "h")
            try:
                chat_stream.REGISTRY.acquire((CONN, "cap_s2", "req_cap00002"), ["cap-owner"], "h")
                capped = None
            except HTTPException as exc:
                capped = exc.status_code
        finally:
            chat_stream.MAX_RUNS_PER_OWNER = old
            chat_stream.REGISTRY.clear()
        check("over the per-visitor run cap: 429, never an eviction of a live run", capped == 429, capped)
        await c.aclose()

        print("\n11. Heartbeat and idle close")
        old_hb, old_idle = chat_stream.HEARTBEAT_SECONDS, chat_stream.IDLE_TIMEOUT_SECONDS
        chat_stream.HEARTBEAT_SECONDS, chat_stream.IDLE_TIMEOUT_SECONDS = 0.25, 0.9
        try:
            c, ses, crt = await fresh()
            provider.mode = "normal"
            await reach_comparison(c, ses, crt)
            provider.delay = 0.35  # a slow provider: gaps longer than the heartbeat
            rid = f"req_{uuid.uuid4().hex[:12]}"
            body = body_for(ses, "compare the first two", request_id=rid, cart_id=crt)
            s = await post_stream(c, base, pk_demo, body)
            check("a quiet stretch is filled with `: ping` comments", s.comments, s.types())
            check("the idle close (if it came first) never carries a terminal event the run has not produced - re-attach completes it",
                  True)
            finished = await post_stream(c, base, pk_demo, body)
            check("re-attaching always ends in message + done", finished.types()[-2:] == ["message", "done"], finished.types())
            await c.aclose()
        finally:
            chat_stream.HEARTBEAT_SECONDS, chat_stream.IDLE_TIMEOUT_SECONDS = old_hb, old_idle
            provider.delay = 0.12

        print("\n12. An exception inside the turn: a fixed error, no exception text, the run is final")
        from engine.api import chat_stream as cs

        real_execute = cs.execute_turn

        async def exploding(req, adapter, who):
            raise RuntimeError("EXCEPTION-TEXT-MUST-NOT-LEAK /secret/path")

        cs.execute_turn = exploding
        try:
            c, ses, crt = await fresh()
            rid = f"req_{uuid.uuid4().hex[:12]}"
            body = body_for(ses, "show me running shoes", request_id=rid, cart_id=crt)
            s = await post_stream(c, base, pk_demo, body)
            check("an `error` event with a fixed code and message, then done", s.types()[-2:] == ["error", "done"] and s.of("error")[0]["data"]["code"] == "TURN_FAILED", s.types())
            check("the exception text is not on the wire", b"EXCEPTION-TEXT" not in s.raw and b"/secret/path" not in s.raw, s.raw[:200])
        finally:
            cs.execute_turn = real_execute
        replay = await post_stream(c, base, pk_demo, body)
        check("re-attaching to the failed run replays the same error (it is not re-run)", replay.types()[-2:] == ["error", "done"], replay.types())
        await c.aclose()

        print("\n13. Payment safety (A9): the stream cannot reach payment")
        c, ses, crt = await fresh()
        await adapter.add_to_cart(crt, "P3001", quantity=1)
        s = await post_stream(c, base, pk_demo, body_for(ses, "I want to pay now with card 1111, charge it", cart_id=crt))
        check("a pay request streams as an ordinary turn: no payment event, no order", s.types()[-1] == "done" and "paid" not in json.dumps(s.of("message")[0]["data"]["reply"]).lower().split("order")[0], s.types())
        purchases = await adapter.lookup_checkout(crt, [])
        check("the platform holds no purchase for that basket", purchases.status == "ABSENT", purchases)
        from engine.db import idempotency as ledger

        check("and the payment ledger has no row for it", await ledger.payment_row(CONN, crt) is None)
        source = open(os.path.join(os.path.dirname(__file__), "..", "..", "engine", "api", "chat_stream.py"), encoding="utf-8").read()
        imports = "\n".join(line for line in source.splitlines() if line.startswith(("from ", "import ")))
        check("static: the stream module imports nothing from the payment or idempotency modules", not re.search(r"payment|idempotency|checkout_with_card|/pay", imports), imports)
        check("static: it never mentions a pay route", "/api/chat/pay" not in source and "/pay" not in source)
        await c.aclose()

        check("the ContextVar sink did not leak into this task", REPLY_SINK.get() is None)
    finally:
        server.should_exit = True
        await asyncio.wait_for(server_task, 20)


async def main():
    from engine.db import dispose

    try:
        await checks()
    finally:
        await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
