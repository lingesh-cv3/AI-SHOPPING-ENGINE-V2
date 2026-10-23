"""Regression: a provider-side tool-schema rejection must not discard a whole
interpretation.

Live root cause (Groq, gpt-oss-120b): the model emitted
`durable_fact_candidates[i].confidence` as a string; the provider validates
tool arguments against OUR schema (`"type": ["number","null"]`) and answered
400 `tool_use_failed` - the entire interpretation was lost for one optional
field. Fixed by (a) accepting a string in the schema (the parser already
coerces) and (b) one bounded regeneration on `tool_use_failed`.

Model-free: no network, no key.

    python tests/shopper_v2/test_phase7_provider_tool_validation.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

failures = 0


def check(label, ok):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    failures += 0 if ok else 1


def _confidence_types():
    from engine.reasoning.prompts import interpretation_tool
    tool = interpretation_tool()
    props = tool["function"]["parameters"]["properties"]
    return props["durable_fact_candidates"]["items"]["properties"]["confidence"]["type"]


async def retry_behaviour():
    import httpx
    from openai import APIStatusError
    from engine.reasoning import llm as llm_mod

    def status_error(body):
        req = httpx.Request("POST", "https://x/y")
        resp = httpx.Response(400, request=req)
        return APIStatusError("bad", response=resp, body=body)

    ok_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=None, tool_calls=[
            SimpleNamespace(function=SimpleNamespace(name="t", arguments="{}"))]))],
        usage=None, model="m",
    )

    def client_with(outcomes):
        calls = {"n": 0}

        async def create(**_):
            o = outcomes[min(calls["n"], len(outcomes) - 1)]
            calls["n"] += 1
            if isinstance(o, Exception):
                raise o
            return o
        c = llm_mod.LLMClient.__new__(llm_mod.LLMClient)
        c.config = SimpleNamespace(model="m", temperature=0, max_tokens=10, base_url="")
        c._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return c, calls

    tuf = {"code": "tool_use_failed", "message": "validation failed"}
    c, calls = client_with([status_error(tuf), ok_response])
    res = await c.complete(system="s", messages=[], tools=[{"type": "function"}], force_tool=True)
    check("one tool_use_failed is regenerated and succeeds", calls["n"] == 2 and res.tool_calls == [{"name": "t", "arguments": {}}])

    c, calls = client_with([status_error(tuf)])
    try:
        await c.complete(system="s", messages=[], tools=[{"type": "function"}], force_tool=True)
        raised = False
    except llm_mod.LLMUnavailable:
        raised = True
    check("a second tool_use_failed degrades (bounded: exactly 2 attempts)", raised and calls["n"] == 2)

    c, calls = client_with([status_error({"code": "invalid_api_key"})])
    try:
        await c.complete(system="s", messages=[], tools=[{"type": "function"}], force_tool=True)
        raised = False
    except llm_mod.LLMUnavailable:
        raised = True
    check("an unrelated 400 is never retried", raised and calls["n"] == 1)


async def payload_shape():
    """OpenAI's newer models (gpt-5/gpt-6 family, o-series) reject the legacy
    `max_tokens` and any non-default `temperature` with a 400 - every turn would
    silently degrade. Older models and Groq keep the legacy shape."""
    from types import SimpleNamespace

    from engine.reasoning import llm as llm_mod

    captured = {}

    async def create(**payload):
        captured.clear()
        captured.update(payload)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None))],
            usage=None, model="m",
        )

    def client(model, base_url):
        c = llm_mod.LLMClient.__new__(llm_mod.LLMClient)
        c.config = SimpleNamespace(model=model, temperature=0.2, max_tokens=1400, base_url=base_url)
        c._client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return c

    oa = "https://api.openai.com/v1"
    for model in ("gpt-6-luna", "gpt-5.6-luna", "o4-mini"):
        await client(model, oa).complete(system="s", messages=[])
        check(f"{model}: max_completion_tokens, no max_tokens", captured.get("max_completion_tokens") == 1400 and "max_tokens" not in captured)
        check(f"{model}: no custom temperature", "temperature" not in captured)
        want = "none" if model.startswith("gpt-6") else "low"  # gpt-6-luna + tools 400s on anything but "none" (measured)
        check(f"{model}: reasoning_effort sent ({want})", captured.get("reasoning_effort") == want)
    await client("gpt-4o-mini", oa).complete(system="s", messages=[])
    check("gpt-4o-mini keeps the legacy shape", captured.get("max_tokens") == 1400 and captured.get("temperature") == 0.2 and "reasoning_effort" not in captured and "extra_body" not in captured)
    await client("openai/gpt-oss-120b", "https://api.groq.com/openai/v1").complete(system="s", messages=[])
    check("Groq keeps the legacy shape (+ its own reasoning_effort)", captured.get("max_tokens") == 1400 and captured.get("temperature") == 0.2 and captured.get("extra_body") == {"reasoning_effort": "low"})


def main():
    print("Interpretation tool schema")
    types = _confidence_types()
    check("fact confidence accepts number, string and null", set(types) == {"number", "string", "null"})
    from engine.reasoning.service import ReasoningService  # noqa: F401
    print("Bounded regeneration on tool_use_failed")
    asyncio.run(retry_behaviour())
    print("Provider payload shape per model family")
    asyncio.run(payload_shape())
    print("\n  " + (f"{failures} FAILURE(S)" if failures else "all provider tool-validation checks passed"))
    sys.exit(1 if failures else 0)


main()
