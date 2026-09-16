import httpx, time, json, uuid, sys

PK_NORTHFIELD = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
PK_KETTLE = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"
BASE = "http://localhost:8000"

def chat(conn, key, session_id, msg):
    client = httpx.Client(cookies={})
    r = client.post(
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
    for i in range(n):
        sid = f"va10-{label}-{uuid.uuid4().hex[:8]}"
        try:
            resp = chat(conn, key, sid, msg)
        except Exception as e:
            print(f"  [{i}] ERROR {e}")
            other += 1
            continue
        reply = resp.get("reply", "")
        products = resp.get("products", [])
        summary = (resp.get("action_summary") or "")
        hedge_markers = ["couldn't confirm", "closest thing", "nothing matched", "couldn't turn anything up", "not something this shop carries"]
        has_hedge = any(m in reply.lower() for m in hedge_markers)
        if products and not has_hedge:
            undisclosed += 1
            print(f"  {label}[{i}] UNDISCLOSED: reply={reply!r} products={[p.get('title') for p in products]} summary={summary!r}", flush=True)
        elif products and has_hedge:
            disclosed_weak += 1
        elif not products:
            honest_decline += 1
        else:
            other += 1
    print(f"{label}: undisclosed={undisclosed} disclosed_weak={disclosed_weak} honest_decline={honest_decline} other={other} / {n}", flush=True)
    return undisclosed

if __name__ == "__main__":
    total_undisclosed = 0
    print("=== Northfield kayak trials ===")
    total_undisclosed += run("conn_demo", PK_NORTHFIELD, "I need a kayak", 10, "kayak-A")
    total_undisclosed += run("conn_demo", PK_NORTHFIELD, "do you sell kayaks?", 10, "kayak-B")
    total_undisclosed += run("conn_demo", PK_NORTHFIELD, "water sports accessories", 10, "kayak-C")
    print("=== Kettle coffee control ===")
    run("conn_kettle", PK_KETTLE, "a fruity coffee", 8, "coffee")
    print(f"\nTOTAL UNDISCLOSED: {total_undisclosed} / 30 kayak-family trials")
