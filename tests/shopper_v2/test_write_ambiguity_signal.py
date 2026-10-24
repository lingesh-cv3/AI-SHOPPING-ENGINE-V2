"""The transport's "an attempt may have been processed" signal (model-free, no network).

A retried write can end with an error that looks conclusive only because an EARLIER attempt of the same call timed out after the
platform acted. The HTTP client flips a shared holder on any such attempt; the payment guard reads it. These tests drive the real
`PlatformClient` over an in-process mock transport.

    python tests/shopper_v2/test_write_ambiguity_signal.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402

import httpx  # noqa: E402

from adapters.framework.http import PlatformClient  # noqa: E402
from shared.models import CommerceError, ErrorCode  # noqa: E402
from shared.models.errors import WRITE_AMBIGUITY, WriteAmbiguity  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


def client(handler):
    c = PlatformClient("http://platform.invalid", connection_id="conn_t", max_attempts=3)
    c._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://platform.invalid")
    return c


async def run(handler, *, wrap=None):
    """Call POST /pay with a fresh holder listening; returns (holder.ambiguous, outcome)."""
    holder = WriteAmbiguity()
    token = WRITE_AMBIGUITY.set(holder)
    try:
        c = client(handler)
        coro = c.request("POST", "/pay", json={}, idempotency_key="k")
        try:
            body = await (wrap(coro) if wrap else coro)
            return holder.ambiguous, body
        except CommerceError as exc:
            return holder.ambiguous, exc
    finally:
        WRITE_AMBIGUITY.reset(token)


async def checks():
    print("1. Clean outcomes are not ambiguous")
    amb, body = await run(lambda req: httpx.Response(200, json={"purchase": {"ref": "O1"}}))
    check("a clean 200 is not ambiguous", amb is False and body == {"purchase": {"ref": "O1"}}, (amb, body))
    amb, exc = await run(lambda req: httpx.Response(404, json={}))
    check("a clean 404 (no such endpoint) is not ambiguous", amb is False and isinstance(exc, CommerceError) and exc.code == ErrorCode.CAPABILITY_UNSUPPORTED, (amb, exc))
    amb, exc = await run(lambda req: httpx.Response(401, json={}))
    check("a clean 401 is not ambiguous", amb is False and isinstance(exc, CommerceError) and exc.code == ErrorCode.AUTH_FAILED, (amb, exc))

    print("\n2. Anything that may have reached the platform is ambiguous")
    calls = {"n": 0}

    def timeout_then_ok(req):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"error": "BASKET_CLOSED"})

    amb, body = await run(timeout_then_ok)
    check("a timeout on attempt 1 then an error BODY on attempt 2 is ambiguous (the body alone looks conclusive)", amb is True and body == {"error": "BASKET_CLOSED"}, (amb, body))
    amb, exc = await run(lambda req: httpx.Response(503, text="down"))
    check("repeated 503s end UPSTREAM_ERROR and are ambiguous", amb is True and isinstance(exc, CommerceError) and exc.code == ErrorCode.UPSTREAM_ERROR, (amb, exc))
    amb, exc = await run(lambda req: httpx.Response(429, text="slow down"))
    check("a 429 is ambiguous too (a gateway can answer 429 after an upstream write)", amb is True and exc.code == ErrorCode.RATE_LIMITED, (amb, exc))
    amb, exc = await run(lambda req: httpx.Response(200, text="<html>not json</html>"))
    check("an unreadable body is ambiguous and surfaces as a normalised error (no raw exception)", amb is True and isinstance(exc, CommerceError) and exc.code == ErrorCode.UPSTREAM_ERROR, (amb, exc))

    def transport_error(req):
        raise httpx.ConnectError("refused", request=req)

    amb, exc = await run(transport_error)
    check("a transport failure is ambiguous", amb is True and isinstance(exc, CommerceError), (amb, exc))

    print("\n3. The signal survives tasks created around the call")
    calls["n"] = 0
    amb, body = await run(timeout_then_ok, wrap=lambda coro: asyncio.wait_for(coro, 30))
    check("inside asyncio.wait_for", amb is True, amb)
    calls["n"] = 0
    amb, body = await run(timeout_then_ok, wrap=lambda coro: asyncio.shield(asyncio.ensure_future(coro)))
    check("inside a shielded child task", amb is True, amb)
    calls["n"] = 0
    amb, body = await run(timeout_then_ok, wrap=lambda coro: asyncio.create_task(coro))
    check("inside a plain child task", amb is True, amb)

    print("\n4. Nobody listening is harmless")
    c = client(lambda req: httpx.Response(503, text="x"))
    try:
        await c.request("POST", "/pay", json={}, idempotency_key="k")
        raised = False
    except CommerceError:
        raised = True
    check("without a holder the client behaves as before", raised and WRITE_AMBIGUITY.get() is None)


async def main():
    await checks()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
