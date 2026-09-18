"""Probe the new /api/v2/chat route's auth and shopper-scoping the same way
auditroutes.py already probes every V1 route - reused pattern, not a new
security model.

Requires the engine running on port 8000 (see CLAUDE.md's "Running it") and
.env.keys present (run mint_keys.py first if not).

    python tests/shopper_v2/test_tenant_isolation_v2.py
"""

import http.cookiejar
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ENGINE = "http://127.0.0.1:8000"


def keys() -> dict:
    path = Path(__file__).resolve().parents[2] / ".env.keys"
    if not path.exists():
        print("  .env.keys not found. Run mint_keys.py first.")
        sys.exit(1)
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip()
    return out


K = keys()
PK_DEMO = K.get("CV3_PUBLISHABLE_CONN_DEMO")
PK_KETTLE = K.get("CV3_PUBLISHABLE_CONN_KETTLE")


def _request(method, path, key=None, body=None, cookiejar_obj=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(ENGINE + path, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    opener = (
        urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookiejar_obj))
        if cookiejar_obj is not None
        else urllib.request.build_opener()
    )
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, {}


def main() -> int:
    failures = 0

    def check(label: str, ok: bool) -> None:
        nonlocal failures
        print(f"  {'ok    ' if ok else 'FAILED'}  {label}")
        if not ok:
            failures += 1

    session_id = f"sess_v2_test_{uuid.uuid4().hex[:12]}"
    body = {
        "connection_id": "conn_demo",
        "session_id": session_id,
        "message": "hello",
    }

    # 1. Refused without a key.
    status, _ = _request("POST", "/api/v2/chat", key=None, body=body)
    check("refused without a key", status == 401)

    # 2. Accepted with the shop's own key (first touch, own cookie jar).
    jar_a = http.cookiejar.CookieJar()
    status, _ = _request("POST", "/api/v2/chat", key=PK_DEMO, body=body, cookiejar_obj=jar_a)
    check("accepted with the shop's own key", status == 200)

    # 3. Refused with another merchant's key.
    status, _ = _request("POST", "/api/v2/chat", key=PK_KETTLE, body=body)
    check("refused with another merchant's key", status in (401, 404))

    # 4. Refused with another shopper's cookie jar, same publishable key -
    #    the scoping question auditroutes.py's own docstring calls out as
    #    "the one that matters".
    jar_b = http.cookiejar.CookieJar()
    status, _ = _request("POST", "/api/v2/chat", key=PK_DEMO, body=body, cookiejar_obj=jar_b)
    check(
        "refused for the same session from a different shopper's browser",
        status == 404,
    )

    print()
    if failures:
        print(f"  {failures} FAILURE(S)")
    else:
        print("  all /api/v2/chat tenant-isolation checks passed")
    print()
    return 1 if failures else 0


sys.exit(main())
