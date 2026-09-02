import json
import urllib.error
import urllib.request
from pathlib import Path

keys = {}
for line in Path(".env.keys").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, _, v = line.partition("=")
        keys[k.strip()] = v.strip()

KEY = keys["CV3_SECRET_CONN_DEMO"]


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:8000{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or "{}")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


print()
_, cart = call("POST", "/api/shop/conn_demo/cart")
cid = cart["cart_id"]
print(f"cart {cid}")

_, c = call(
    "POST",
    f"/api/shop/conn_demo/cart/{cid}/lines",
    {"product_id": "P1001", "variant_id": "P1001-8", "quantity": 1},
)
line_id = c["lines"][0]["line_id"]
print(f"added, line {line_id}, {c['item_count']} item(s)")

status, body = call(
    "PATCH",
    f"/api/shop/conn_demo/cart/{cid}/lines/{line_id}",
    {"quantity": 0},
)
print(f"\nPATCH returned {status}")
print(f"  {body}")