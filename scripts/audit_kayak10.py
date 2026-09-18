"""10 kayak-family trials on Northfield, for value-auditor regression check
against the d230d35 category-validation fix (fallback branch only touched
Kettle's RECOMMEND_PRODUCTS wide-catalog path, but re-check anyway)."""
import httpx, time, uuid

PK_NORTHFIELD = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
BASE = "http://localhost:8000"

def chat(conn, key, session_id, msg):
    r = httpx.post(
        f"{BASE}/api/chat",
        json={"connection_id": conn, "session_id": session_id, "message": msg},
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    return r.json()

def run(conn, key, msg, n, label):
    undisclosed = 0
    disclosed_weak = 0
    honest_decline = 0
    other = 0
    hedge_markers = ["couldn't confirm", "closest thing", "nothing matched", "couldn't turn anything up", "not something this shop carries"]
    for i in range(n):
        sid = f"audit-kayak10-{label}-{uuid.uuid4().hex[:8]}"
        try:
            resp = chat(conn, key, sid, msg)
        except Exception as e:
            print(f"  [{i}] ERROR {e}")
            other += 1
            continue
        reply = resp.get("reply", "")
        products = resp.get("products", [])
        has_hedge = any(m in reply.lower() for m in hedge_markers)
        if products and not has_hedge:
            undisclosed += 1
            print(f"  {label}[{i}] UNDISCLOSED: reply={reply!r} products={[p.get('title') for p in products]}")
        elif products and has_hedge:
            disclosed_weak += 1
        elif not products:
            honest_decline += 1
        else:
            other += 1
        time.sleep(1.5)
    print(f"{label}: undisclosed={undisclosed} disclosed_weak={disclosed_weak} honest_decline={honest_decline} other={other} / {n}")
    return undisclosed

if __name__ == "__main__":
    total = 0
    # Spread 10 total trials across the three established kayak-family phrasings (4/3/3)
    total += run("conn_demo", PK_NORTHFIELD, "I need a kayak", 4, "kayak-A")
    total += run("conn_demo", PK_NORTHFIELD, "do you sell kayaks?", 3, "kayak-B")
    total += run("conn_demo", PK_NORTHFIELD, "water sports accessories", 3, "kayak-C")
    print(f"\nTOTAL UNDISCLOSED: {total} / 10 kayak-family trials")
