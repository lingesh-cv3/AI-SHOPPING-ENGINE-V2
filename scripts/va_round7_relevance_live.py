import httpx, json, uuid, sys, time

BASE = "http://localhost:8000"
NF_KEY = None
KB_KEY = None
for line in open(r"D:\CV3\AI_SHOPPING_ENGINE\.env.keys"):
    if line.startswith("CV3_PUBLISHABLE_CONN_DEMO="):
        NF_KEY = line.strip().split("=", 1)[1]
    if line.startswith("CV3_PUBLISHABLE_CONN_KETTLE="):
        KB_KEY = line.strip().split("=", 1)[1]

def chat(conn, key, message, n=1):
    results = []
    for i in range(n):
        sid = f"va7-{uuid.uuid4().hex[:10]}"
        r = httpx.post(f"{BASE}/api/chat", json={
            "connection_id": conn, "session_id": sid, "message": message
        }, headers={"Authorization": f"Bearer {key}"}, timeout=30)
        try:
            j = r.json()
        except Exception:
            j = {"error": r.text}
        results.append(j.get("reply", j))
        time.sleep(3)
    return results

if len(sys.argv) > 1 and sys.argv[1] == "kayak":
    print("=== Northfield: 'do you sell kayaks?' x8 ===")
    for out in chat("conn_demo", NF_KEY, "do you sell kayaks?", 8):
        print("-", out[:160])
elif len(sys.argv) > 1 and sys.argv[1] == "coffee":
    print("=== Kettle: 'a fruity coffee' x5 ===")
    for out in chat("conn_kettle", KB_KEY, "a fruity coffee", 5):
        print("-", out[:160])
    print("=== Kettle: 'something smooth and not too bitter' x5 ===")
    for out in chat("conn_kettle", KB_KEY, "something smooth and not too bitter", 5):
        print("-", out[:160])
