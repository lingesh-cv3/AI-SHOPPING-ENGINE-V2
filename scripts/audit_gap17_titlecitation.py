"""Follow-up probe: does an exact-title-citation of an IRRELEVANT product
bypass query_diverges_from_said entirely? Reproduces the 'supplies for a
backyard barbecue' -> undisclosed confident 'Insulated Water Bottle 750ml'
hit found in the padding-shape batch."""
import httpx, time, uuid, json

BASE = "http://localhost:8000"
PK_NF = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
PK_KT = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"

HEDGE_MARKERS = [
    "couldn't confirm", "closest thing", "nothing matched",
    "couldn't turn anything up", "not something this shop carries",
]

def chat(conn, key, msg):
    sid = f"audit17b-{uuid.uuid4().hex[:8]}"
    r = httpx.post(f"{BASE}/api/chat", json={"connection_id": conn, "session_id": sid, "message": msg},
                   headers={"Authorization": f"Bearer {key}"}, timeout=40)
    return r.json()

def run(conn, key, msg, n, label):
    confident = hedged = declined = 0
    for i in range(n):
        resp = chat(conn, key, msg)
        reply = (resp.get("reply") or "")
        products = resp.get("products") or []
        has_hedge = any(m in reply.lower() for m in HEDGE_MARKERS)
        if products and not has_hedge:
            confident += 1
            print(f"  {label}[{i}] CONFIDENT titles={[p.get('title') for p in products]} reply={reply[:90]!r}")
        elif products and has_hedge:
            hedged += 1
        else:
            declined += 1
        time.sleep(2)
    print(f"== {label}: confident={confident} hedged={hedged} declined={declined} / {n}\n")
    return confident

if __name__ == "__main__":
    run("conn_demo", PK_NF, "supplies for a backyard barbecue", 10, "nf-bbq-followup")
    run("conn_kettle", PK_KT, "something to buy for a housewarming party", 10, "kt-housewarming")
