"""Shared HTTP machinery for platform adapters.

Every adapter needs the same handful of things: a client with sane timeouts,
retries on transient failure, idempotency-key plumbing, and structured logging
that never records credentials. Doing that once here means a new platform
adapter is mapping logic and nothing else.

The one opinionated decision in this module: request() returns the parsed JSON
body and does *not* interpret it. Deciding whether a body represents success is
platform-specific - a platform that returns HTTP 200 with an error key looks
identical to a successful call at the transport layer. That judgement belongs to
the adapter, which is why it is not made here.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from shared.models import CommerceError, ErrorCode

logger = logging.getLogger(__name__)

#: Transport-level failures worth retrying. A 4xx means we asked wrongly and
#: asking again identically will fail identically, so those are never retried.
_RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class PlatformClient:
    """An HTTP client bound to one merchant connection.

    Credentials live here and are attached to outgoing requests. They are never
    logged, never returned in an error, and never reachable by the AI Engine -
    the engine holds a StandardCommerceInterface, not a client.
    """

    def __init__(
        self,
        base_url: str,
        *,
        connection_id: str,
        headers: dict[str, str] | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.connection_id = connection_id
        self.max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers or {},
            timeout=timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Make a request, retrying transient failures.

        Returns the parsed JSON body without judging it. Raises CommerceError
        only for transport and protocol failures - never for a business outcome,
        because at this layer we cannot tell the difference.
        """
        headers: dict[str, str] = {}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key

        last_error: CommerceError | None = None

        for attempt in range(1, self.max_attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._client.request(
                    method, path, params=params, json=json, headers=headers
                )
                elapsed_ms = int((time.perf_counter() - started) * 1000)

                if response.status_code in _RETRYABLE_STATUS:
                    last_error = CommerceError(
                        ErrorCode.RATE_LIMITED
                        if response.status_code == 429
                        else ErrorCode.UPSTREAM_ERROR,
                        f"platform returned {response.status_code}",
                        platform_detail=response.text[:500],
                    )
                    self._log(method, path, response.status_code, elapsed_ms, attempt)
                    await self._backoff(attempt)
                    continue

                if response.status_code in (401, 403):
                    raise CommerceError(
                        ErrorCode.AUTH_FAILED,
                        "platform rejected our credentials",
                        retryable=False,
                    )

                if response.status_code == 404:
                    # A 404 here means the *endpoint* is absent, not that a
                    # product is missing - a missing product is a business
                    # outcome the platform reports in its body. An absent
                    # endpoint means the adapter claimed a capability the
                    # platform does not have.
                    raise CommerceError(
                        ErrorCode.CAPABILITY_UNSUPPORTED,
                        f"no endpoint at {path}",
                        retryable=False,
                    )

                self._log(method, path, response.status_code, elapsed_ms, attempt)
                return response.json()

            except httpx.TimeoutException as exc:
                last_error = CommerceError(
                    ErrorCode.TIMEOUT, f"timeout calling {path}", platform_detail=str(exc)
                )
                await self._backoff(attempt)
            except httpx.HTTPError as exc:
                last_error = CommerceError(
                    ErrorCode.UPSTREAM_ERROR,
                    f"transport failure calling {path}",
                    platform_detail=str(exc),
                )
                await self._backoff(attempt)

        raise last_error or CommerceError(
            ErrorCode.UPSTREAM_ERROR, f"exhausted {self.max_attempts} attempts on {path}"
        )

    async def _backoff(self, attempt: int) -> None:
        """Exponential backoff. Skipped after the final attempt."""
        if attempt < self.max_attempts:
            await asyncio.sleep(0.2 * (2 ** (attempt - 1)))

    def _log(
        self, method: str, path: str, status: int, elapsed_ms: int, attempt: int
    ) -> None:
        """Structured log. Deliberately excludes headers and bodies.

        Credentials travel in headers and shopper data travels in bodies, so
        neither is logged. Path and status are enough to diagnose an integration
        problem.
        """
        logger.info(
            "platform_call",
            extra={
                "connection_id": self.connection_id,
                "method": method,
                "path": path,
                "status": status,
                "latency_ms": elapsed_ms,
                "attempt": attempt,
            },
        )