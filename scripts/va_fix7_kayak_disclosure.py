"""Live verification for the kayak weak-match disclosure fix.

Sends 'do you sell kayaks?' to Northfield several times and reports
whether any trial that shows the water-bottle (or similar weak) match
now carries the honest disclosure sentence instead of confident framing.
"""
import sys
import time
import uuid

import httpx

BASE = "http://localhost:8000"
KEY = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
CONN = "conn_demo"

TRIALS = int(sys.argv[1]) if len(sys.argv) > 1 else 6

for i in range(TRIALS):
    sid = f"va-kayak-disc-{uuid.uuid4().hex[:8]}"
    r = httpx.post(
        f"{BASE}/api/chat",
        json={
            "connection_id": CONN,
            "session_id": sid,
            "message": "do you sell kayaks?",
        },
        headers={"Authorization": f"Bearer {KEY}"},
        timeout=30.0,
    )
    if r.status_code != 200:
        print(f"[{i+1}] HTTP {r.status_code}: {r.text[:200]}")
        continue
    data = r.json()
    reply = data.get("reply", "")
    products = data.get("products") or []
    titles = [p.get("title") for p in products]
    disclosed = "couldn't confirm an exact match" in reply.lower()
    print(f"[{i+1}] products={titles} disclosed={disclosed}")
    print(f"      reply: {reply[:160]!r}")
    time.sleep(2)
