"""A scriptable, local, OpenAI-compatible fake model provider for browser tests of the chat stream. No network, no key, no real model.

Point an engine at it with `OPENAI_BASE_URL=http://127.0.0.1:<port>/v1`, `OPENAI_API_KEY=<anything>`, `LLM_PROVIDER=openai`. It answers exactly the model calls a streamed
comparison turn makes:

  interpret_conversation  -> the next queued claim (queue them with POST /__script before sending the shopper's message); none queued -> HTTP 500 (the engine degrades)
  write_synthesis         -> a STREAM, one token-sized fragment every `delay` seconds, with the grounding fields first and the reply last (the order the streaming tool asks for)
  anything else           -> HTTP 500 (the engine's own fallbacks apply)

It records when the synthesis stream started and finished (wall-clock seconds), so a test can prove the shopper saw text WHILE the provider was still writing:
GET /__state -> {"stream_started_at", "stream_finished_at", "stream_requests", "claims_left"}.

    python tests/support/fake_openai_provider.py --port 18998
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()

REPLY = "The Trailblazer is the more cushioned pick while the Starter Road is lighter and cheaper for short runs on pavement."

STATE: dict = {"claims": [], "mode": "normal", "delay": 0.25, "reply": REPLY, "stream_started_at": None, "stream_finished_at": None, "stream_requests": 0}


def _chunk(delta: dict) -> bytes:
    body = {"id": "chatcmpl-fake", "object": "chat.completion.chunk", "model": "fake-model", "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    return b"data: " + json.dumps(body).encode() + b"\n\n"


def _tool_delta(name: str | None, args: str) -> dict:
    fn: dict = {"arguments": args}
    tc: dict = {"index": 0, "function": fn}
    if name:
        fn["name"] = name
        tc.update({"id": "call_0", "type": "function"})
    return {"tool_calls": [tc]}


def _whole(name: str, arguments: dict) -> dict:
    return {"model": "fake-model", "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_0", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]}}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5}}


def _split(text: str, n: int) -> list[str]:
    size = max(1, len(text) // n)
    return [text[i:i + size] for i in range(0, len(text), size)]


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/__script")
async def script(request: Request) -> dict:
    body = await request.json()
    if body.get("reset"):
        STATE.update({"claims": [], "mode": "normal", "delay": 0.25, "reply": REPLY, "stream_started_at": None, "stream_finished_at": None, "stream_requests": 0})
    STATE["claims"].extend(body.get("claims", []))
    for key in ("mode", "delay", "reply"):
        if key in body:
            STATE[key] = body[key]
    return {"claims_left": len(STATE["claims"])}


@app.get("/__state")
async def state() -> dict:
    return {k: STATE[k] for k in ("stream_started_at", "stream_finished_at", "stream_requests")} | {"claims_left": len(STATE["claims"])}


@app.post("/v1/chat/completions")
async def completions(request: Request):
    payload = await request.json()
    name = ((payload.get("tools") or [{}])[0].get("function") or {}).get("name")
    if name == "interpret_conversation" and STATE["claims"]:
        return JSONResponse(_whole(name, STATE["claims"].pop(0)))
    if name != "write_synthesis":
        return JSONResponse({"error": {"message": "the fake provider does not answer this call"}}, status_code=500)

    mode, reply = STATE["mode"], STATE["reply"]
    doc = {"personalizes": False, "cited_reason": None, "reply": reply}
    if mode == "ungrounded":
        doc = {"personalizes": True, "cited_reason": "an invented preference phrase", "reply": reply}
    elif mode == "reply_first":
        doc = {"reply": reply, "personalizes": False, "cited_reason": None}
    if not payload.get("stream"):
        return JSONResponse(_whole("write_synthesis", doc))

    text = json.dumps(doc)
    fragments = _split(text, 16)
    STATE["stream_requests"] += 1

    async def body():
        STATE["stream_started_at"] = time.time()
        STATE["stream_finished_at"] = None
        yield _chunk(_tool_delta("write_synthesis", ""))
        for fragment in fragments:
            yield _chunk(_tool_delta(None, fragment))
            await asyncio.sleep(float(STATE["delay"]))
        STATE["stream_finished_at"] = time.time()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18998)
    args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
