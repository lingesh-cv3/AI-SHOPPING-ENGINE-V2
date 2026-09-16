"""Live verification the divergence-check widening doesn't regress the
Kettle coffee cases (the exact case a prior relevance fix broke once)."""
import sys
import time
import uuid

import httpx

BASE = "http://localhost:8000"
KEY = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"
CONN = "conn_kettle"

MESSAGES = [
    "a fruity coffee",
    "something smooth and not too bitter",
]
TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 5

for msg in MESSAGES:
    print(f"=== {msg!r} ===")
    for i in range(TRIALS):
        sid = f"va-coffee-reg-{uuid.uuid4().hex[:8]}"
        r = httpx.post(
            f"{BASE}/api/chat",
            json={"connection_id": CONN, "session_id": sid, "message": msg},
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=30.0,
        )
        if r.status_code != 200:
            print(f"[{i+1}] HTTP {r.status_code}: {r.text[:200]}")
            continue
        data = r.json()
        products = data.get("products") or []
        titles = [p.get("title") for p in products]
        print(f"[{i+1}] products={titles} reply={data.get('reply','')[:100]!r}")
        time.sleep(2)
