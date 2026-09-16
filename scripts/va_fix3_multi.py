"""Verify the multi-item extra-item detection fix at 3 and 4 named items. Scratch, gitignored."""
import json
import urllib.request
import uuid

NORTHFIELD_PK = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
BASE = "http://127.0.0.1:8000"


def post(path, pk, body):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {pk}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def chat(pk, conn, sid, message, cart_id=None):
    body = {"connection_id": conn, "session_id": sid, "message": message}
    if cart_id:
        body["cart_id"] = cart_id
    return post("/api/chat", pk, body)


sid = f"va3m-{uuid.uuid4().hex[:8]}"
cart = post("/api/shop/conn_demo/cart", NORTHFIELD_PK, {})
cart_id = cart["cart_id"]
print("cart_id:", cart_id)

msg = (
    "Add the Trailblazer Running Shoe size 8, the Marathon Pro Racing Shoe "
    "size 8, and the Fell Runner Trail Shoe size 8"
)
resp = chat(NORTHFIELD_PK, "conn_demo", sid, msg, cart_id=cart_id)
print("3-item reply:", resp.get("reply"))
print("choices:", resp.get("choices"))
print("full keys:", list(resp.keys()))
print(json.dumps(resp, indent=2)[:3000])
