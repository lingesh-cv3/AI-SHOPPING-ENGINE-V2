"""Live pagination/relevance retest, throttle-respecting (sleeps between calls)."""
import http.cookiejar
import json
import time
import urllib.request
import uuid

NORTHFIELD_PK = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
KETTLE_PK = "cv3_pk_3c2c9096d90b453cd2a54c5fce9587b20b041c364e243eb8083ad540eca69d25"
BASE = "http://127.0.0.1:8000"


def make_opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def post(opener, pk, path, body):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {pk}"},
        method="POST",
    )
    with opener.open(req, timeout=90) as r:
        return json.loads(r.read())


def one_turn(pk, conn, msg):
    opener = make_opener()
    sid = f"va-pg-{uuid.uuid4().hex[:8]}"
    cart = post(opener, pk, f"/api/shop/{conn}/cart", {})
    cid = cart["cart_id"]
    t0 = time.time()
    r = post(opener, pk, "/api/chat", {"connection_id": conn, "session_id": sid, "message": msg, "cart_id": cid})
    dt = time.time() - t0
    return r, dt


print("=== Northfield: 'do you sell kayaks?' x5 ===")
for i in range(5):
    try:
        r, dt = one_turn(NORTHFIELD_PK, "conn_demo", "do you sell kayaks?")
        print(f"trial {i+1}: {dt:.1f}s reply={r.get('reply')!r} action={r.get('action_taken')}")
    except Exception as e:
        print(f"trial {i+1}: ERROR {e}")
    time.sleep(16)

print("\n=== Kettle: 'a fruity coffee' x5 ===")
for i in range(5):
    try:
        r, dt = one_turn(KETTLE_PK, "conn_kettle", "a fruity coffee")
        print(f"trial {i+1}: {dt:.1f}s reply={r.get('reply')!r} action={r.get('action_taken')}")
    except Exception as e:
        print(f"trial {i+1}: ERROR {e}")
    time.sleep(16)

print("\n=== Kettle: 'something smooth and not too bitter' x5 ===")
for i in range(5):
    try:
        r, dt = one_turn(KETTLE_PK, "conn_kettle", "something smooth and not too bitter")
        print(f"trial {i+1}: {dt:.1f}s reply={r.get('reply')!r} action={r.get('action_taken')}")
    except Exception as e:
        print(f"trial {i+1}: ERROR {e}")
    time.sleep(16)
