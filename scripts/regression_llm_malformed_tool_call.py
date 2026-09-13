"""Regression check for the pre-existing engine/reasoning/llm.py logging
crash found during the Shopper Phase 1+2 audit and fixed in the hardening
pass (see PROGRESS.md).

Not integrated into healthcheck.py/fuzz.py/auditroutes.py: those three are
deliberately HTTP-only, driving the real running services rather than
importing engine modules directly (see CLAUDE.md's "The constraint" and
each suite's own docstring) - and a malformed tool-call response is a
provider-response-shape condition that cannot be reliably forced on demand
through a live model call. This is a pure unit-level reproduction instead:
run by hand (`python scripts/regression_llm_malformed_tool_call.py`) after
any future change to engine/reasoning/llm.py's tool-call parsing.
"""
import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.reasoning.llm import LLMClient, LLMConfig


async def main() -> None:
    client = LLMClient(LLMConfig(api_key="fake", model="fake-model"))

    # The exact response shape that crashed before the fix: a tool call
    # whose arguments are not valid JSON.
    fake_call = SimpleNamespace(
        function=SimpleNamespace(name="propose_actions", arguments="{not valid json")
    )
    fake_message = SimpleNamespace(content=None, tool_calls=[fake_call])
    fake_choice = SimpleNamespace(message=fake_message)
    fake_response = SimpleNamespace(choices=[fake_choice], usage=None, model="fake-model")

    async def fake_create(**kwargs):
        return fake_response

    client._client.chat.completions.create = fake_create

    result = await client.complete(system="sys", messages=[{"role": "user", "content": "hi"}])
    assert result.tool_calls == [], result.tool_calls
    print("PASS: malformed tool call discarded gracefully, no crash:", result)


if __name__ == "__main__":
    asyncio.run(main())
