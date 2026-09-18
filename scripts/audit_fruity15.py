"""Independent 15-trial batch of 'a fruity coffee' on Kettle, for value-auditor verification."""
import time
import uuid

import httpx

BASE = "http://localhost:8000"
KEY = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"
CONN = "conn_kettle"
MSG = "a fruity coffee"
TRIALS = 15

fails = 0
for i in range(TRIALS):
    sid = f"audit-fruity15-{uuid.uuid4().hex[:8]}"
    r = httpx.post(
        f"{BASE}/api/chat",
        json={"connection_id": CONN, "session_id": sid, "message": MSG},
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

print(f"\nSUMMARY: {TRIALS-fails}/{TRIALS} passed, {fails} failed")
