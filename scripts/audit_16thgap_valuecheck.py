"""Adversarial round-17 check on commit 7600a91 (subset-based
query_diverges_from_said). Read-only value-audit trials, not a committed
test. See value-auditor invocation for what each block is checking."""
import httpx, time, uuid, sys, json

BASE = "http://localhost:8000"
PK_NF = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
PK_KT = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"

HEDGE_MARKERS = [
    "couldn't confirm", "closest thing", "nothing matched",
    "couldn't turn anything up", "not something this shop carries",
    "not sure this is exactly", "not certain", "may not be exactly",
    "isn't a perfect match", "not a perfect match", "best i could find",
    "not exactly what", "unable to confirm",
]

def chat(conn, key, msg, sid=None):
    sid = sid or f"audit17-{uuid.uuid4().hex[:8]}"
    t0 = time.perf_counter()
    r = httpx.post(
        f"{BASE}/api/chat",
        json={"connection_id": conn, "session_id": sid, "message": msg},
        headers={"Authorization": f"Bearer {key}"},
        timeout=40,
    )
    dt = time.perf_counter() - t0
    try:
        data = r.json()
    except Exception:
        data = {"reply": f"HTTP {r.status_code}: {r.text[:200]}"}
    return data, dt

def run(conn, key, msg, n, label, sleep=2.0):
    undisclosed = 0
    disclosed = 0
    declined = 0
    other = 0
    lat = []
    for i in range(n):
        try:
            resp, dt = chat(conn, key, msg)
            lat.append(dt)
        except Exception as e:
            print(f"  {label}[{i}] ERROR {e}")
            other += 1
            continue
        reply = (resp.get("reply") or "")
        products = resp.get("products") or []
        has_hedge = any(m in reply.lower() for m in HEDGE_MARKERS)
        if products and not has_hedge:
            undisclosed += 1
            print(f"  {label}[{i}] CONFIDENT titles={[p.get('title') for p in products]} reply={reply[:90]!r} ({dt:.1f}s)")
        elif products and has_hedge:
            disclosed += 1
            print(f"  {label}[{i}] HEDGED titles={[p.get('title') for p in products]} reply={reply[:90]!r} ({dt:.1f}s)")
        elif not products:
            declined += 1
            print(f"  {label}[{i}] DECLINED reply={reply[:90]!r} ({dt:.1f}s)")
        else:
            other += 1
        time.sleep(sleep)
    avg_lat = sum(lat) / len(lat) if lat else 0.0
    print(f"== {label} ({msg!r}): confident={undisclosed} hedged={disclosed} declined={declined} other={other} / {n}  avg_latency={avg_lat:.1f}s\n")
    return dict(confident=undisclosed, hedged=disclosed, declined=declined, other=other, n=n, avg_latency=avg_lat)


if __name__ == "__main__":
    section = sys.argv[1] if len(sys.argv) > 1 else "all"
    results = {}

    if section in ("1", "all"):
        print("### TASK 1: 'water sports accessories' dedicated 15-trial weight, Northfield\n")
        results["water_sports_15"] = run("conn_demo", PK_NF, "water sports accessories", 15, "water-sports-15")

    if section in ("2", "all"):
        print("### TASK 2: fresh same-shape padding phrasings\n")
        results["nf_scuba"] = run("conn_demo", PK_NF, "accessories for scuba diving", 5, "nf-scuba")
        results["nf_bbq"] = run("conn_demo", PK_NF, "supplies for a backyard barbecue", 5, "nf-bbq")
        results["kt_kitchen"] = run("conn_kettle", PK_KT, "cleaning supplies for my kitchen", 5, "kt-kitchen")
        results["kt_picnic"] = run("conn_kettle", PK_KT, "supplies for a picnic in the park", 5, "kt-picnic")

    if section in ("3", "all"):
        print("### TASK 3: over-hedging check - genuine paraphrase/synonym cases\n")
        results["nf_feet"] = run("conn_demo", PK_NF, "something to keep my feet comfortable on long runs", 5, "nf-feet")
        results["nf_sore"] = run("conn_demo", PK_NF, "something for my sore muscles after a workout", 5, "nf-sore")
        results["kt_awake"] = run("conn_kettle", PK_KT, "something to help me stay awake in the morning", 5, "kt-awake")
        results["kt_stomach"] = run("conn_kettle", PK_KT, "a coffee that's easy on the stomach", 5, "kt-stomach")
        print("### TASK 3 controls\n")
        results["fruity10"] = run("conn_kettle", PK_KT, "a fruity coffee", 10, "fruity-10")
        results["shoes5"] = run("conn_demo", PK_NF, "show me your shoes", 5, "shoes-5")
        results["smooth5"] = run("conn_kettle", PK_KT, "something smooth and not too bitter", 5, "smooth-5")

    print("\n\n=== SUMMARY JSON ===")
    print(json.dumps(results, indent=2))
