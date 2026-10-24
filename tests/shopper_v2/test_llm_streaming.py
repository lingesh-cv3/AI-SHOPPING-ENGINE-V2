"""Real provider streaming in the LLM client (model-free: a fake OpenAI-compatible provider over an in-process transport; no network, no key).

Asserted: reply text is forwarded WHILE the provider is still generating (timestamps, not just order); only the `reply` field of the reply tool is
forwarded (never hidden reasoning, `cited_reason`, other tools, or provider errors); the assembled result equals the non-streaming result; a stream that
dies, a malformed chunk and a failing consumer degrade safely; and a call with no consumer is byte-identical to before streaming existed.

    python tests/shopper_v2/test_llm_streaming.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_llm_streaming.db"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import httpx  # noqa: E402
from openai import AsyncOpenAI  # noqa: E402

from engine.reasoning.llm import LLMClient, LLMConfig, LLMUnavailable  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got!r}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def sse(obj) -> bytes:
    return b"data: " + (obj if isinstance(obj, bytes) else json.dumps(obj).encode()) + b"\n\n"


def tool_chunk(index=0, name=None, args=None, call_id=None):
    fn = {}
    if name:
        fn["name"] = name
    if args is not None:
        fn["arguments"] = args
    tc = {"index": index, "function": fn}
    if call_id:
        tc["id"] = call_id
        tc["type"] = "function"
    return {"id": "c1", "object": "chat.completion.chunk", "model": "fake-model", "choices": [{"index": 0, "delta": {"tool_calls": [tc]}, "finish_reason": None}]}


def content_chunk(text):
    return {"id": "c1", "object": "chat.completion.chunk", "model": "fake-model", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}


def reasoning_chunk(text, field="reasoning"):
    return {"id": "c1", "object": "chat.completion.chunk", "model": "fake-model", "choices": [{"index": 0, "delta": {field: text}, "finish_reason": None}]}


def usage_chunk():
    return {"id": "c1", "object": "chat.completion.chunk", "model": "fake-model", "choices": [], "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}


class Provider:
    """A fake provider: records every request, and streams (with delays) or answers whole."""

    def __init__(self, chunks=None, *, delay=0.0, fail_after=None, whole=None):
        self.chunks = chunks or []
        self.delay = delay
        self.fail_after = fail_after
        self.whole = whole
        self.requests = []
        self.stream_done_at = None

    async def _body(self):
        for i, c in enumerate(self.chunks):
            if self.fail_after is not None and i == self.fail_after:
                raise httpx.ReadError("provider connection reset: RAW-PROVIDER-SECRET")
            yield sse(c)
            if self.delay:
                await asyncio.sleep(self.delay)
        self.stream_done_at = time.monotonic()  # recorded BEFORE [DONE]: the SDK stops reading there and never resumes this generator
        yield b"data: [DONE]\n\n"

    def handler(self):
        async def handle(request: httpx.Request) -> httpx.Response:
            payload = json.loads(request.content)
            self.requests.append(payload)
            if payload.get("stream"):
                return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=self._body())
            return httpx.Response(200, json=self.whole)
        return handle


def client_for(provider: Provider) -> LLMClient:
    client = LLMClient(LLMConfig(api_key="fake", base_url="http://provider.invalid/v1", model="fake-model"))
    client._client = AsyncOpenAI(api_key="fake", base_url="http://provider.invalid/v1", max_retries=0,
                                 http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider.handler())))
    return client


TOOLS = [{"type": "function", "function": {"name": "write_synthesis", "description": "d", "parameters": {"type": "object", "properties": {}}}}]
REPLY = 'Here are two pairs: "light" socks\nand a £ sign \U0001F45F.'
ARGS = json.dumps({"cited_reason": "SECRET GROUNDING you asked for socks", "reply": REPLY, "personalizes": False})
#: The same call with the reply written FIRST - the order that gives the shopper text soonest.
ARGS_REPLY_FIRST = json.dumps({"reply": REPLY, "cited_reason": "SECRET GROUNDING you asked for socks", "personalizes": False})


def split(text, n):
    size = max(1, len(text) // n)
    return [text[i:i + size] for i in range(0, len(text), size)]


async def checks():
    print("1. Reply text is forwarded as the provider generates it; the result equals the non-streaming one")
    chunks = [tool_chunk(0, "write_synthesis", "", "call_1")] + [tool_chunk(0, args=f) for f in split(ARGS, 9)] + [usage_chunk()]
    provider = Provider(chunks)
    got, stamps = [], []

    async def consumer(text):
        got.append(text)
        stamps.append(time.monotonic())

    result = await client_for(provider).complete(system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=True, on_reply_delta=consumer)
    check("the request asked the provider to stream", provider.requests[0].get("stream") is True, provider.requests[0].keys())
    check("the forwarded deltas concatenate to exactly the reply", "".join(got) == REPLY, "".join(got))
    check("more than one delta was forwarded (it really streamed)", len(got) > 1, got)
    check("cited_reason and the JSON syntax never reach the consumer", "SECRET" not in "".join(got) and "cited_reason" not in "".join(got) and "{" not in "".join(got), got)
    whole = {"model": "fake-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "write_synthesis", "arguments": ARGS}}]}}], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}
    plain = await client_for(Provider(whole=whole)).complete(system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=True)
    check("the assembled result equals the non-streaming result (tool calls)", result.tool_calls == plain.tool_calls and result.tool_calls[0]["arguments"]["reply"] == REPLY, (result.tool_calls, plain.tool_calls))
    check("usage and model are carried through the stream", result.prompt_tokens == 11 and result.completion_tokens == 7 and result.model == "fake-model", (result.prompt_tokens, result.model))

    print("\n2. Delivery is incremental IN TIME: the first text arrives while the provider is still generating")
    for label, doc in (("reply written first", ARGS_REPLY_FIRST), ("cited_reason written first", ARGS)):
        chunks = [tool_chunk(0, "write_synthesis", "", "call_1")] + [tool_chunk(0, args=f) for f in split(doc, 10)]
        provider = Provider(chunks, delay=0.12)
        got, stamps = [], []
        t0 = time.monotonic()
        await client_for(provider).complete(system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=True, on_reply_delta=consumer)
        first = stamps[0] - t0
        done = provider.stream_done_at - t0
        check(f"{label}: first text arrives before the provider has finished generating", first < done - 0.3, (round(first, 2), round(done, 2)))
        check(f"{label}: later deltas keep arriving over time, not in one burst", stamps[-1] - stamps[0] > 0.2, round(stamps[-1] - stamps[0], 2))
        if label == "reply written first":
            first_fast = first
        else:
            first_slow = first
    check("the key order the model writes decides how soon text appears (reply first is sooner)", first_fast < first_slow, (round(first_fast, 2), round(first_slow, 2)))

    print("\n3. Hidden reasoning, other tools and prose are never forwarded when the call forces a tool")
    chunks = [
        reasoning_chunk("THINKING about what the shopper wants", "reasoning"),
        reasoning_chunk("more hidden thoughts", "reasoning_content"),
        tool_chunk(0, "other_tool", "", "call_a"), tool_chunk(0, args='{"reply": "OTHER TOOL REPLY MUST NOT STREAM"}'),
        tool_chunk(1, "write_synthesis", "", "call_b"), *[tool_chunk(1, args=f) for f in split(ARGS, 5)],
        content_chunk("prose beside a forced tool"),
    ]
    got.clear()
    result = await client_for(Provider(chunks)).complete(system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=True, on_reply_delta=consumer)
    text = "".join(got)
    check("only the reply tool's reply field is forwarded", text == REPLY, text)
    check("hidden reasoning never forwarded", "THINKING" not in text and "hidden thoughts" not in text, text)
    check("another tool's arguments never forwarded", "OTHER TOOL" not in text, text)
    check("prose beside a forced tool is not forwarded (the tool's reply is the answer)", "prose beside" not in text, text)
    check("but the result still carries both tool calls for the engine", {c["name"] for c in result.tool_calls} == {"other_tool", "write_synthesis"} and "THINKING" not in json.dumps(result.tool_calls), result.tool_calls)

    print("\n4. A call that is not forced to use a tool streams its prose")
    got.clear()
    result = await client_for(Provider([content_chunk("Hel"), content_chunk("lo "), content_chunk("there")])).complete(
        system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=False, on_reply_delta=consumer)
    check("prose deltas forwarded and assembled", "".join(got) == "Hello there" and result.text == "Hello there", (got, result.text))

    print("\n5. A stream that dies, a malformed chunk and a failing consumer degrade safely")
    chunks = [tool_chunk(0, "write_synthesis", "", "call_1")] + [tool_chunk(0, args=f) for f in split(ARGS_REPLY_FIRST, 8)]
    got.clear()
    try:
        await client_for(Provider(chunks, fail_after=4)).complete(system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=True, on_reply_delta=consumer)
        raised, message = False, ""
    except LLMUnavailable as exc:
        raised, message = True, str(exc)
    check("a mid-stream failure raises LLMUnavailable (the caller falls back)", raised, message)
    check("the provider's raw error text is not carried in the exception", "RAW-PROVIDER-SECRET" not in message, message)
    check("text delivered before the failure was delivered (the caller resets it)", len(got) >= 1, got)
    try:
        await client_for(Provider([b"{not json at all"])).complete(system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=True, on_reply_delta=consumer)
        raised = False
    except LLMUnavailable:
        raised = True
    check("a malformed provider chunk raises LLMUnavailable, never forwarded", raised)

    calls = {"n": 0}

    async def exploding(text):
        calls["n"] += 1
        raise RuntimeError("consumer bug")

    chunks = [tool_chunk(0, "write_synthesis", "", "call_1")] + [tool_chunk(0, args=f) for f in split(ARGS, 6)]
    result = await client_for(Provider(chunks)).complete(system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=True, on_reply_delta=exploding)
    check("a consumer that raises does not break the model call", calls["n"] > 0 and result.tool_calls[0]["arguments"]["reply"] == REPLY, result.tool_calls)

    print("\n6. With no consumer the request is exactly what it was before streaming existed")
    provider = Provider(whole=whole)
    await client_for(provider).complete(system="s", messages=[{"role": "user", "content": "q"}], tools=TOOLS, force_tool=True)
    req = provider.requests[0]
    check("no stream flag and no stream options in the payload", "stream" not in req and "stream_options" not in req, sorted(req))
    check("the same keys as always", {"model", "temperature", "max_tokens", "messages", "tools", "tool_choice"} <= set(req), sorted(req))

    print("\n7. synthesize() asks for streaming ONLY when a sink is set")
    from engine.reasoning import ReasoningService
    from engine.reasoning.stream_sink import REPLY_SINK

    class Sink:
        def __init__(self):
            self.events = []
            self._n = 0

        def begin(self, policy, items):
            self._n += 1
            self.events.append(("begin", self._n))
            return self._n

        async def field(self, segment, name, value):
            self.events.append(("field", segment, name))

        async def delta(self, segment, text):
            self.events.append(("delta", segment, text))

        async def end(self, segment, ok):
            self.events.append(("end", segment, ok))

    syn_args = json.dumps({"reply": REPLY, "cited_reason": "from the catalogue", "personalizes": False})
    chunks = [tool_chunk(0, "write_synthesis", "", "call_1")] + [tool_chunk(0, args=f) for f in split(syn_args, 6)]
    provider = Provider(chunks)
    service = ReasoningService(client_for(provider))
    sink = Sink()
    token = REPLY_SINK.set(sink)
    try:
        synthesis = await service.synthesize(kind="comparison", message="socks", items=[{"product_id": "P1", "title": "Sock"}], stream_policy="cited_reason_check")
    finally:
        REPLY_SINK.reset(token)
    deltas = "".join(e[2] for e in sink.events if e[0] == "delta")
    check("with a sink: it streamed, the sink saw begin, the reply text, and a successful end", provider.requests[0].get("stream") is True and deltas == REPLY and sink.events[0] == ("begin", 1) and sink.events[-1] == ("end", 1, True), sink.events[:3])
    check("and the synthesis the engine checks is the complete text", synthesis.text == REPLY and synthesis.cited_reason == "from the catalogue", (synthesis.text, synthesis.cited_reason))
    whole_syn = {"model": "fake-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "c", "type": "function", "function": {"name": "write_synthesis", "arguments": syn_args}}]}}]}
    provider = Provider(whole=whole_syn)
    plain = await ReasoningService(client_for(provider)).synthesize(kind="discovery", message="socks", items=[{"product_id": "P1", "title": "Sock"}])
    check("with NO sink: not streamed, the same synthesis", "stream" not in provider.requests[0] and plain.text == REPLY and plain.cited_reason == "from the catalogue", sorted(provider.requests[0]))
    # A sink alone is not enough: a call site that does not declare a stream_policy (discovery - the engine discards its prose) is never streamed.
    sink = Sink()
    token = REPLY_SINK.set(sink)
    try:
        provider = Provider(whole=whole_syn)
        undeclared = await ReasoningService(client_for(provider)).synthesize(kind="discovery", message="socks", items=[{"product_id": "P1", "title": "Sock"}])
    finally:
        REPLY_SINK.reset(token)
    check("a sink but NO stream_policy: not streamed and the sink hears nothing", "stream" not in provider.requests[0] and sink.events == [] and undeclared.text == REPLY, (sorted(provider.requests[0]), sink.events))
    sink = Sink()
    token = REPLY_SINK.set(sink)
    try:
        broken = ReasoningService(client_for(Provider(chunks, fail_after=3)))
        failed = await broken.synthesize(kind="comparison", message="socks", items=[], stream_policy="cited_reason_check")
    finally:
        REPLY_SINK.reset(token)
    check("a failing stream: no synthesis text, and the sink is told to discard the draft", failed.text is None and sink.events[-1] == ("end", 1, False), sink.events)


if __name__ == "__main__":
    asyncio.run(checks())
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)
