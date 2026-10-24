"""The LLM client.

The only module in the engine that knows a model provider exists. Everything
above it receives list[ProposedAction] and a Diagnosis, and cannot tell whether
those came from a model, a placeholder, or a rules table.

Groq is used through the openai SDK rather than Groq's own, because their API is
OpenAI-compatible and that keeps the provider swappable. Moving to Anthropic or
OpenAI later means changing a base URL and a model name, not rewriting the
reasoning layer.

Two operational realities shaped this file:

Models get retired. Groq has deprecated a model roughly every two months, and
llama-3.3-70b-versatile was shut down on 16 August 2026. The model ID is
therefore configuration, never a constant in code.

Free-tier limits bite on tokens per minute, not requests per day. A shopper turn
carrying real commerce context can be a few thousand tokens, so the per-minute
ceiling arrives long before the daily one. Rate-limit responses are read from the
response headers rather than assumed, because published limits go stale quickly.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from openai import APIStatusError, AsyncOpenAI, RateLimitError

from .stream_parse import ReplyExtractor

logger = logging.getLogger(__name__)

#: Groq's OpenAI-compatible endpoint.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

#: OpenAI's own endpoint.
OPENAI_BASE_URL = "https://api.openai.com/v1"

#: Groq's recommended replacement for the retired Llama 3.3 70B. A production
#: model rather than a preview one, and it supports tool calling, which the
#: reasoning layer depends on.
DEFAULT_MODEL = "openai/gpt-oss-120b"

#: OpenAI's own cheap tool-calling model.
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"


class LLMUnavailable(Exception):
    """The model could not be reached, or refused the request.

    Deliberately a plain exception rather than a CommerceError: this is not a
    commerce failure, it is our own dependency failing. The reasoning service
    catches it and falls back, so a shopper never sees a turn fail because a model
    provider was busy.
    """

    def __init__(self, message: str, *, retry_after_seconds: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


@dataclass
class LLMConfig:
    """Everything about the provider, read from the environment.

    No key ever appears in code or in a committed file. from_env raises if the key
    is missing rather than silently constructing a client that will fail on first
    use with a confusing error.
    """

    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = GROQ_BASE_URL
    #: Low, because we want the model to reason consistently rather than
    #: creatively. Two identical shopper situations should not produce wildly
    #: different proposals.
    temperature: float = 0.2
    #: Ceiling for one completion. The interpretation tool call carries a
    #: full `shopping_state` and `secondary_operation`, so it must not be
    #: starved: at a 1200 ceiling with DEFAULT reasoning effort the model's
    #: hidden reasoning consumed the budget and the JSON was cut off before
    #: its closing brace (Groq `tool_use_failed`, indistinguishable from a
    #: rate limit in the trace). The real fix is `reasoning_effort` (see
    #: `complete()`), which cut a typical call from ~550 to ~350 completion
    #: tokens - this ceiling then leaves >3x headroom while keeping the
    #: per-call tokens-per-minute reservation small.
    max_tokens: int = 1400
    timeout_seconds: float = 8.0

    @classmethod
    def _provider(cls) -> str:
        """Which provider to use, read once so both `from_env` and `available`
        agree on it.

        Two providers exist for two different reasons, not interchangeably:
        OpenAI is what the shopper-facing engine runs on day to day (real
        token cost, but no per-minute throttle to fight); Groq is what this
        session's own testing and healthcheck runs use, specifically so
        verifying a change never spends the user's OpenAI credits. Defaults
        to "openai" - the engine, run by the user, should use OpenAI unless
        something deliberately overrides it. Testing invocations set
        `LLM_PROVIDER=groq` in their own shell/process environment rather
        than touching this default.
        """
        return (os.getenv("LLM_PROVIDER", "openai").strip().lower() or "openai")

    @classmethod
    def from_env(cls) -> LLMConfig:
        if cls._provider() == "groq":
            key = os.getenv("GROQ_API_KEY", "").strip()
            if not key:
                raise LLMUnavailable(
                    "GROQ_API_KEY is not set. Add it to .env at the project "
                    "root, or set LLM_PROVIDER=openai to use OpenAI instead."
                )
            return cls(
                api_key=key,
                model=os.getenv("GROQ_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
                base_url=os.getenv("GROQ_BASE_URL", GROQ_BASE_URL).strip()
                or GROQ_BASE_URL,
            )

        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            raise LLMUnavailable(
                "OPENAI_API_KEY is not set. Add it to .env at the project "
                "root, or set LLM_PROVIDER=groq to use Groq instead."
            )
        return cls(
            api_key=key,
            model=os.getenv("OPENAI_MODEL", DEFAULT_OPENAI_MODEL).strip()
            or DEFAULT_OPENAI_MODEL,
            base_url=os.getenv("OPENAI_BASE_URL", OPENAI_BASE_URL).strip()
            or OPENAI_BASE_URL,
        )

    @classmethod
    def available(cls) -> bool:
        """Whether a key is configured for whichever provider is selected.
        Used to decide whether to use the AI at all, so the engine still runs
        with rules when it is not."""
        if cls._provider() == "groq":
            return bool(os.getenv("GROQ_API_KEY", "").strip())
        return bool(os.getenv("OPENAI_API_KEY", "").strip())


@dataclass
class LLMResult:
    """One completion. Tool calls and text are kept separate.

    tool_calls is where structured proposals arrive. text is the conversational
    reply. A turn can legitimately produce either, both, or neither, so neither is
    treated as required.
    """

    text: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    model: str | None = None


class LLMClient:
    """Thin wrapper over the provider. No prompts, no commerce knowledge.

    Prompt construction lives in the reasoning service; this class only knows how
    to send messages and interpret a response. That split means prompts can be
    tested without a network call and transport can be tested without caring what
    the prompts say.
    """

    def __init__(self, config: LLMConfig | None = None) -> None:
        self.config = config or LLMConfig.from_env()
        self._client = AsyncOpenAI(
            api_key=self.config.api_key,
            base_url=self.config.base_url,
            timeout=self.config.timeout_seconds,
            # One retry only. A shopper is waiting, and a slow correct answer is
            # worse than a fast fallback to rules.
            max_retries=0,
        )

    async def complete(
        self,
        *,
        system: str,
        messages: list[dict[str, str]],
        tools: list[dict[str, Any]] | None = None,
        force_tool: bool = False,
        on_reply_delta: Callable[[str], Awaitable[None] | None] | None = None,
        reply_tool: str = "write_synthesis",
        reply_field: str = "reply",
        on_field: Callable[[str, str | None], Awaitable[None] | None] | None = None,
        field_names: tuple[str, ...] = ("cited_reason",),
    ) -> LLMResult:
        """Send a completion request.

        The system prompt is passed separately and kept byte-identical between
        turns, because Groq caches repeated prefixes at half the input rate and
        cached tokens do not count toward rate limits. Variable context belongs in
        the user messages, after the cached prefix.

        `on_reply_delta` turns on REAL provider streaming (`stream=True`) for this call and is invoked with each new piece of the
        shopper-visible text AS THE MODEL WRITES IT: the decoded characters of the `reply_field` string inside the `reply_tool`
        tool call's arguments (or, for a call that is not forced to use a tool, plain `delta.content`). Nothing else is ever
        forwarded - not hidden reasoning tokens, not other tool names or arguments (a `cited_reason` stays inside the engine),
        not provider errors. The returned `LLMResult` is assembled exactly as the non-streaming path assembles it, so every
        caller's downstream checks are unchanged. Left at None (the default) the request, the payload and the result are
        byte-identical to what they were before streaming existed.

        `on_field` lets the engine's own gate learn an INTERNAL field of the same tool call (default `cited_reason`) the moment it
        is complete - its text, or None when the model wrote null or a non-string - so it can decide whether the reply may be
        shown BEFORE releasing any of it. The field's value is never forwarded to `on_reply_delta`; it goes only to this callback.
        """
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        # OpenAI's reasoning-era models (gpt-5/gpt-6 family, o-series) reject
        # the legacy `max_tokens` and any non-default `temperature` with a 400
        # - which would degrade every turn to "no model". They take
        # `max_completion_tokens` (hidden reasoning tokens count against it,
        # as with Groq's gpt-oss) and a `reasoning_effort`. Detected by model
        # family on OpenAI's own endpoint only; Groq and older OpenAI models
        # keep the legacy shape unchanged.
        if _is_openai_reasoning_model(self.config.base_url, self.config.model):
            payload["max_completion_tokens"] = payload.pop("max_tokens")
            payload.pop("temperature", None)
            # Measured (2026-10): `gpt-6-luna` on /v1/chat/completions rejects
            # function tools unless reasoning_effort is exactly "none" (omitting
            # it or sending "low" both 400). Structured extraction needs no
            # hidden reasoning anyway, so "none" is also the cheapest. Other
            # reasoning-era families (unverified with tools) keep "low".
            default_effort = "none" if (self.config.model or "").lower().startswith("gpt-6") else "low"
            effort = os.getenv("LLM_REASONING_EFFORT", default_effort).strip()
            if effort:
                payload["reasoning_effort"] = effort
        # Groq's gpt-oss models are reasoning models: hidden reasoning tokens
        # are billed against `max_tokens` AND the tokens-per-minute limit.
        # Measured (2026-09): default effort spent ~550 completion tokens on
        # a compact tool call (and, at a 1200 ceiling, was the real cause of
        # the truncated-JSON `tool_use_failed` failures - reasoning ate the
        # budget before the JSON closed); effort=low spent ~350 with the
        # same classification. Structured extraction does not need deep
        # reasoning. Only sent where the provider/model accepts it -
        # OpenAI chat models reject the parameter. Overridable per process
        # via LLM_REASONING_EFFORT ("" disables).
        if "groq" in (self.config.base_url or "") and "gpt-oss" in (self.config.model or ""):
            effort = os.getenv("LLM_REASONING_EFFORT", "low").strip()
            if effort:
                payload["extra_body"] = {"reasoning_effort": effort}
        if tools:
            payload["tools"] = tools
            # "required" makes the model call a tool rather than replying in
            # prose. Used when we need structured proposals and nothing else.
            payload["tool_choice"] = "required" if force_tool else "auto"
        streaming = on_reply_delta is not None
        if streaming:
            payload["stream"] = True
            if "api.openai.com" in (self.config.base_url or ""):
                payload["stream_options"] = {"include_usage": True}

        # Opt-in, bounded Retry-After wait. A provider 429 states when it will
        # accept the request again; on a tokens-per-minute tier that is often
        # only seconds away. Default 0 = degrade immediately (a shopper is
        # waiting). A deployment on a tight tier, or a verification run
        # measuring real-provider success, sets LLM_RATE_LIMIT_WAIT_SECONDS
        # to the longest total wait it will accept; the wait is never
        # honoured beyond that cap.
        wait_cap = float(os.getenv("LLM_RATE_LIMIT_WAIT_SECONDS", "0") or 0)
        waited_total = 0.0
        tool_retry_done = False
        while True:
            try:
                response = await self._client.chat.completions.create(**payload)
                if streaming:
                    try:
                        return await self._read_stream(
                            response, on_reply_delta=on_reply_delta, reply_tool=reply_tool,
                            reply_field=reply_field, forward_prose=not force_tool,
                            on_field=on_field, field_names=field_names,
                        )
                    except asyncio.CancelledError:
                        raise
                    except LLMUnavailable:
                        raise
                    except Exception as exc:  # noqa: BLE001 - a stream that dies mid-way is the provider failing
                        raise LLMUnavailable(f"the model stream was interrupted: {type(exc).__name__}") from exc
                break
            except RateLimitError as exc:
                retry_after = _retry_after(exc)
                if (
                    wait_cap > 0
                    and retry_after is not None
                    and waited_total + retry_after + 0.5 <= wait_cap
                ):
                    await asyncio.sleep(retry_after + 0.5)
                    waited_total += retry_after + 0.5
                    continue
                raise LLMUnavailable(
                    "rate limited by the model provider",
                    retry_after_seconds=retry_after,
                ) from exc
            except APIStatusError as exc:
                # A 404 here almost always means the model ID was retired. Worth
                # saying so plainly, because the generic message is unhelpful.
                hint = ""
                # Server-side only (never shown to a shopper): the provider's
                # own error code/message is what tells a malformed generation
                # (`tool_use_failed`) from a bad request or a retired model.
                logger.warning(
                    "provider error %s: %s",
                    exc.status_code,
                    str(getattr(exc, "body", None))[:500],
                )
                # A `tool_use_failed` 400 is the model's own malformed
                # generation (schema-invalid arguments), not a bad request:
                # sampling is stochastic, so one regeneration is a sound
                # response. Bounded to one - never a loop.
                if (
                    exc.status_code == 400
                    and not tool_retry_done
                    and "tool_use_failed" in str(getattr(exc, "body", ""))
                ):
                    tool_retry_done = True
                    continue
                if exc.status_code == 404:
                    hint = (
                        f" - model '{self.config.model}' may have been retired. "
                        "Check the provider's deprecation page."
                    )
                raise LLMUnavailable(f"provider returned {exc.status_code}{hint}") from exc
            except Exception as exc:  # noqa: BLE001 - transport, DNS, timeouts
                raise LLMUnavailable(f"could not reach the model provider: {exc}") from exc

        choice = response.choices[0].message
        usage = getattr(response, "usage", None)
        return self._assemble(
            text=choice.content,
            raw_calls=[(c.function.name, c.function.arguments or "{}") for c in (choice.tool_calls or [])],
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            model=response.model,
        )

    @staticmethod
    def _assemble(
        *,
        text: str | None,
        raw_calls: list[tuple[str, str]],
        prompt_tokens: int | None,
        completion_tokens: int | None,
        model: str | None,
    ) -> LLMResult:
        """Build the `LLMResult`. Shared by the streaming and non-streaming paths so they cannot drift apart."""
        calls: list[dict[str, Any]] = []

        for call_name, call_args in raw_calls:
            try:
                arguments = json.loads(call_args or "{}")
            except json.JSONDecodeError:
                # Open models do occasionally emit malformed JSON. Skip the call
                # rather than crashing the turn - a proposal we cannot parse is
                # simply a proposal we did not receive.
                # `extra={"name": ...}` collides with LogRecord's own
                # reserved `name` attribute and raises `KeyError: "Attempt
                # to overwrite 'name' in LogRecord"` - turning this
                # designed-to-be-recoverable "skip the bad call" path into
                # an unhandled 500 for the whole turn. Pre-existing bug,
                # found and fixed in the Shopper hardening pass (see
                # PROGRESS.md); `tool_call_name` carries the same
                # information without the collision.
                logger.warning(
                    "discarded unparseable tool call",
                    extra={"tool_call_name": call_name},
                )
                continue
            calls.append({"name": call_name, "arguments": arguments})

        return LLMResult(
            text=text,
            tool_calls=calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            model=model,
        )

    async def _read_stream(
        self,
        stream: Any,
        *,
        on_reply_delta: Callable[[str], Awaitable[None] | None] | None,
        reply_tool: str,
        reply_field: str,
        forward_prose: bool,
        on_field: Callable[[str, str | None], Awaitable[None] | None] | None = None,
        field_names: tuple[str, ...] = (),
    ) -> LLMResult:
        """Consume a provider stream, forwarding ONLY the reply text, and return the same result a non-streaming call would.

        Hidden reasoning (`reasoning` / `reasoning_content` deltas) is never read. Tool-call argument fragments are accumulated for
        the result, and the `reply_field` of the `reply_tool` call is decoded incrementally and forwarded; any other tool, and any
        other field of that tool, stays inside the engine. A chunk that does not have the expected shape is skipped (counted in
        the log), never forwarded.
        """
        text_parts: list[str] = []
        calls: dict[int, dict[str, Any]] = {}
        extractors: dict[int, ReplyExtractor] = {}
        field_extractors: dict[tuple[int, str], ReplyExtractor] = {}
        reported: set[tuple[int, str]] = set()
        usage = None
        model = None
        malformed = 0
        try:
            async for chunk in stream:
                try:
                    model = getattr(chunk, "model", None) or model
                    chunk_usage = getattr(chunk, "usage", None)
                    if chunk_usage is not None:
                        usage = chunk_usage
                    choices = getattr(chunk, "choices", None) or []
                    if not choices:
                        continue
                    delta = choices[0].delta
                except Exception:  # noqa: BLE001 - an odd chunk is skipped, not fatal
                    malformed += 1
                    continue
                content = getattr(delta, "content", None)
                if isinstance(content, str) and content:
                    text_parts.append(content)
                    if forward_prose:
                        await self._forward(on_reply_delta, content)
                for tool_delta in getattr(delta, "tool_calls", None) or []:
                    index = getattr(tool_delta, "index", 0) or 0
                    slot = calls.setdefault(index, {"name": "", "args": []})
                    function = getattr(tool_delta, "function", None)
                    if function is None:
                        continue
                    name = getattr(function, "name", None)
                    if name and not slot["name"]:
                        slot["name"] = name
                    fragment = getattr(function, "arguments", None)
                    if isinstance(fragment, str) and fragment:
                        slot["args"].append(fragment)
                        if slot["name"] == reply_tool:
                            # internal fields first: the gate must know about a citation before any reply text in the same fragment
                            if on_field is not None:
                                for field_name in field_names:
                                    key = (index, field_name)
                                    probe = field_extractors.setdefault(key, ReplyExtractor(field_name))
                                    probe.feed(fragment)
                                    if probe.resolved and key not in reported:
                                        reported.add(key)
                                        value = probe.text.strip() if probe.value_kind == "string" else None
                                        if value is not None and len(value) > 2000:
                                            value = value[:2000] + "(truncated)"  # can never be found in a product's text: the gate withholds
                                        await self._forward_field(on_field, field_name, value or None)
                            extractor = extractors.setdefault(index, ReplyExtractor(reply_field))
                            piece = extractor.feed(fragment)
                            if piece:
                                await self._forward(on_reply_delta, piece)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                try:
                    result = close()
                    if inspect.isawaitable(result):
                        await result
                except Exception:  # noqa: BLE001
                    pass
        if malformed:
            logger.warning("skipped %d malformed stream chunk(s)", malformed)
        return self._assemble(
            text="".join(text_parts) or None,
            raw_calls=[(slot["name"], "".join(slot["args"])) for _i, slot in sorted(calls.items()) if slot["name"]],
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            model=model,
        )

    @staticmethod
    async def _forward_field(callback: Callable[[str, str | None], Awaitable[None] | None], name: str, value: str | None) -> None:
        """Tell the engine's gate about a completed internal field. A gate that raises must never break the model call."""
        try:
            result = callback(name, value)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("stream field consumer failed", exc_info=True)

    @staticmethod
    async def _forward(callback: Callable[[str], Awaitable[None] | None] | None, text: str) -> None:
        """Call the consumer of streamed text. A consumer that raises must never break the model call."""
        if callback is None or not text:
            return
        try:
            result = callback(text)
            if inspect.isawaitable(result):
                await result
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.warning("stream consumer failed", exc_info=True)


_OPENAI_REASONING_PREFIXES = ("gpt-5", "gpt-6", "o1", "o3", "o4")


def _is_openai_reasoning_model(base_url: str | None, model: str | None) -> bool:
    return "api.openai.com" in (base_url or "") and (model or "").lower().startswith(
        _OPENAI_REASONING_PREFIXES
    )


def _retry_after(exc: Exception) -> float | None:
    """Read the provider's own retry hint.

    Read from headers rather than assumed, because published free-tier limits go
    stale fast - one tracker measured a ceiling that changed within two months.
    The headers are authoritative; a hardcoded number is a guess.
    """
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or {}
    for key in ("retry-after", "x-ratelimit-reset-tokens", "x-ratelimit-reset-requests"):
        raw = headers.get(key)
        if not raw:
            continue
        try:
            return float(str(raw).rstrip("s"))
        except ValueError:
            continue
    return None