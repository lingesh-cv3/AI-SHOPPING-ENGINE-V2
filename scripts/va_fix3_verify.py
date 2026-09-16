"""Ad hoc verification of the third-round structural fixes. Scratch, gitignored."""
import json
import urllib.request
import uuid

NORTHFIELD_PK = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
KETTLE_PK = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"
BASE = "http://127.0.0.1:8000"


def chat(conn, pk, session_id, message):
    req = urllib.request.Request(
        f"{BASE}/api/chat",
        data=json.dumps(
            {"connection_id": conn, "session_id": session_id, "message": message}
        ).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {pk}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def run(label, conn, pk, message, n=1):
    for i in range(n):
        sid = f"va3-{uuid.uuid4().hex[:8]}"
        try:
            resp = chat(conn, pk, sid, message)
            print(f"[{label} #{i+1}] reply: {resp.get('reply')!r}")
            print(f"    products: {[p.get('title') for p in resp.get('products', [])]}")
        except Exception as e:
            body = getattr(e, "read", lambda: b"")()
            print(f"[{label} #{i+1}] ERROR: {e} {body}")


print("=== Kayak (Northfield) - should honestly degrade ===")
run("kayak", "conn_demo", NORTHFIELD_PK, "do you sell kayaks?", n=5)

print("\n=== Tent (Northfield) - should honestly degrade ===")
run("tent", "conn_demo", NORTHFIELD_PK, "I need a tent for camping", n=3)

print("\n=== Fruity coffee (Kettle) - must still work ===")
run("fruity-coffee", "conn_kettle", KETTLE_PK, "a fruity coffee", n=5)

print("\n=== Smooth not bitter (Kettle) - must still work ===")
run("smooth-coffee", "conn_kettle", KETTLE_PK, "something smooth and not too bitter", n=5)
