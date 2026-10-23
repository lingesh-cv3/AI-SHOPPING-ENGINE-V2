"""`interpret_conversation` tool-schema nullability
(`engine/reasoning/prompts.py::interpretation_tool`).

Live-reproduced defect this closes (2026-09): a direct provider call with
the exact interpretation prompt/tool schema returned HTTP 400 - "Tool call
validation failed: parameters for tool interpret_conversation did not
match schema: errors: [`/reference_attribute`: expected string, but got
null]" - even though `reference_attribute` is NOT in the tool's own
`required` list and `ReasoningService.interpret()`'s own parsing already
treats a non-string value as `None` (the field's real contract, per
`InterpretationResult.reference_attribute: str | None`). The provider's
structured-output validator enforces the DECLARED type strictly against
whatever value is present, even for an optional field - a bare
`"type": "string"` rejects a literal `null`, collapsing the ENTIRE tool
call (every other field too), not just that one. `LLMClient.complete()`
then reports this as the same generic `LLMUnavailable` a genuine 429
does, so the symptom looked identical to rate-limiting from the caller's
side.

Model-free: builds a fake `LLMClient` returning a canned tool call with
`null` for every optional field (the exact shape the live provider
produced) and asserts `ReasoningService.interpret()` parses it into a
real, non-`None` `InterpretationResult` - proving the SERVICE-side
contract already tolerated this; only the SCHEMA needed the fix.

    python tests/shopper_v2/test_phase19_interpretation_schema_nullability.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

failures = 0


def check(label: str, ok: bool) -> None:
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
    if not ok:
        failures += 1


class _FakeLLMClient:
    """Returns a canned tool call, never touches the network - the exact
    shape the live provider generated for "what else?" once every
    optional field it declined to fill came back as a literal `null`
    rather than being omitted."""

    def __init__(self, arguments: dict):
        self._arguments = arguments

    async def complete(self, *, system, messages, tools=None, force_tool=False):
        from engine.reasoning.llm import LLMResult
        return LLMResult(
            text=None,
            tool_calls=[{"name": "interpret_conversation", "arguments": self._arguments}],
        )


def test_null_optional_fields_are_accepted() -> None:
    from engine.reasoning.service import ReasoningService

    arguments = {
        "capability": "discover",
        "context_action": "continue",
        "rationale": "the shopper wants more of the same",
        "context_target_label": None,
        "new_topic_label": None,
        "reference_scope": None,
        "reference_kind": None,
        "reference_positions": None,
        "reference_attribute": None,
        "reference_direction": None,
        "reference_names": None,
        "question_type": "alternatives",
        "secondary_reference": None,
        "cart_operation": None,
        "durable_fact_candidates": None,
        "conversational_repair": None,
        "clarify_if_uncertain": None,
    }
    service = ReasoningService(client=_FakeLLMClient(arguments))
    result = asyncio.run(service.interpret(message="what else?", history=[]))
    check("a null-heavy tool call still parses into a real result, not None", result is not None)
    if result is not None:
        check("reference_attribute: null degrades to None, not a crash", result.reference_attribute is None)
        check("optional enum fields default honestly when null", result.reference_scope == "none")
        check("optional array fields default to empty, never None, when null", result.reference_positions == [])
        check("secondary_reference: null stays None, never fabricated", result.secondary_reference is None)
        check("question_type still reads the real, non-null value provided", result.question_type == "alternatives")


def test_null_secondary_reference_attribute_is_accepted() -> None:
    from engine.reasoning.service import ReasoningService

    arguments = {
        "capability": "compare",
        "context_action": "continue",
        "rationale": "comparing two things",
        "question_type": "relationship",
        "reference_attribute": "price",
        "secondary_reference": {
            "reference_scope": "paused_topic",
            "context_target_label": None,
            "reference_kind": None,
            "reference_positions": None,
            "reference_attribute": None,
            "reference_direction": None,
            "reference_names": None,
        },
    }
    service = ReasoningService(client=_FakeLLMClient(arguments))
    result = asyncio.run(service.interpret(message="which is cheaper", history=[]))
    check("a claim with a null secondary reference_attribute still parses", result is not None)
    if result is not None:
        check("secondary_reference itself is real, not discarded", result.secondary_reference is not None)
        if result.secondary_reference is not None:
            check(
                "secondary_reference.reference_attribute: null degrades to None",
                result.secondary_reference.reference_attribute is None,
            )


def test_schema_declares_nullable_optional_fields() -> None:
    from engine.reasoning.prompts import interpretation_tool

    params = interpretation_tool()["function"]["parameters"]
    required = set(params["required"])
    props = params["properties"]

    def is_nullable(schema: dict) -> bool:
        t = schema.get("type")
        type_ok = isinstance(t, list) and "null" in t
        enum = schema.get("enum")
        enum_ok = enum is None or None in enum
        return type_ok and enum_ok

    for name, schema in props.items():
        if name in required:
            continue
        check(f"optional field {name!r} declares a nullable schema", is_nullable(schema))

    sec_props = props["secondary_reference"]["properties"]
    for name, schema in sec_props.items():
        check(f"secondary_reference.{name!r} declares a nullable schema", is_nullable(schema))

    fact_props = props["durable_fact_candidates"]["items"]["properties"]
    fact_required = set(props["durable_fact_candidates"]["items"]["required"])
    for name, schema in fact_props.items():
        if name in fact_required:
            continue
        check(f"durable_fact_candidates item field {name!r} declares a nullable schema", is_nullable(schema))

    # Required fields deliberately stay strict - the model must always
    # supply a real, confident value for these, never degrade to null.
    for name in required:
        schema = props[name]
        t = schema.get("type")
        check(f"required field {name!r} stays strictly typed (no null)", t == "string")


def main() -> None:
    test_schema_declares_nullable_optional_fields()
    test_null_optional_fields_are_accepted()
    test_null_secondary_reference_attribute_is_accepted()
    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
        raise SystemExit(1)
    print("  all ok")


if __name__ == "__main__":
    main()
