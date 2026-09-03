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

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from openai import APIStatusError, AsyncOpenAI, RateLimitError

logger = logging.getLogger(__name__)

#: Groq's OpenAI-compatible endpoint.
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

#: Groq's recommended replacement for the retired Llama 3.3 70B. A production
#: model rather than a preview one, and it supports tool calling, which the
#: reasoning layer depends on.
DEFAULT_MODEL = "openai/gpt-oss-120b"


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
    max_tokens: int = 1200
    timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls) -> LLMConfig:
        key = os.getenv("GROQ_API_KEY", "").strip()
        if not key:
            raise LLMUnavailable(
                "GROQ_API_KEY is not set. Add it to .env at the project root."
            )
        return cls(
            api_key=key,
            model=os.getenv("GROQ_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL,
            base_url=os.getenv("GROQ_BASE_URL", GROQ_BASE_URL).strip() or GROQ_BASE_URL,
        )

    @classmethod
    def available(cls) -> bool:
        """Whether a key is configured. Used to decide whether to use the AI at
        all, so the engine still runs with rules when it is not."""
        return bool(os.getenv("GROQ_API_KEY", "").strip())


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
        if tools:
            payload["tools"] = tools
            # "required" makes the model call a tool rather than replying in
            # prose. Used when we need structured proposals and nothing else.
            payload["tool_choice"] = "required" if force_tool else "auto"

        try:
            response = await self._client.chat.completions.create(**payload)
        except RateLimitError as exc:
            raise LLMUnavailable(
                "rate limited by the model provider",
                retry_after_seconds=_retry_after(exc),
            ) from exc
        except APIStatusError as exc:
            # A 404 here almost always means the model ID was retired. Worth
            # saying so plainly, because the generic message is unhelpful.
            hint = ""
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
                logger.warning(
                    "discarded unparseable tool call",
                    extra={"name": call.function.name},
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