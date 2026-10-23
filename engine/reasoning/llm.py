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
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from openai import APIStatusError, AsyncOpenAI, RateLimitError

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
    ) -> LLMResult:
        """Send a completion request.

        The system prompt is passed separately and kept byte-identical between
        turns, because Groq caches repeated prefixes at half the input rate and
        cached tokens do not count toward rate limits. Variable context belongs in
        the user messages, after the cached prefix.
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
        calls: list[dict[str, Any]] = []

        for call in choice.tool_calls or []:
            try:
                arguments = json.loads(call.function.arguments or "{}")
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
                    extra={"tool_call_name": call.function.name},
                )
                continue
            calls.append({"name": call.function.name, "arguments": arguments})

        usage = getattr(response, "usage", None)
        return LLMResult(
            text=choice.content,
            tool_calls=calls,
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            model=response.model,
        )


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