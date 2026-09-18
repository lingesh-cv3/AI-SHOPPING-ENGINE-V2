"""Generalization check: two NEW coffee-preference phrasings, not the debugged
'fruity' or 'smooth and not too bitter' ones, on Kettle."""
import time
import uuid

import httpx

BASE = "http://localhost:8000"
KEY = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"
CONN = "conn_kettle"

MESSAGES = [
    "a coffee with chocolate notes",
    "a bold, full-bodied coffee",
]
TRIALS = 4

for msg in MESSAGES:
    print(f"=== {msg!r} ===")
    fails = 0
    for i in range(TRIALS):
        sid = f"audit-newphr-{uuid.uuid4().hex[:8]}"
        r = httpx.post(
            f"{BASE}/api/chat",
            json={"connection_id": CONN, "session_id": sid, "message": msg},
            headers={"Authorization": f"Bearer {KEY}"},
            timeout=30.0,
        )
        if r.status_code != 200:
            print(f"[{i+1}] HTTP {r.status_code}: {r.text[:200]}")
            fails += 1
            continue
        data = r.json()
        products = data.get("products") or []
        titles = [p.get("title") for p in products]
        ok = len(titles) > 0
        if not ok:
            fails += 1
        print(f"[{i+1}] {'PASS' if ok else 'FAIL'} products={titles} reply={data.get('reply','')[:100]!r}")
        time.sleep(2)
    print(f"  -> {TRIALS-fails}/{TRIALS} passed\n")
