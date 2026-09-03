import json
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
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or "{}")


cart = call("POST", "/api/shop/conn_demo/cart")
print(f"\ncart {cart['cart_id']}")

r = call(
    "POST",
    "/api/chat",
    {
        "connection_id": "conn_demo",
        "session_id": "probe_choices",
        "message": "add the Trailblazer Running Shoe to my cart",
        "cart_id": cart["cart_id"],
    },
)

print(f"\nused_model  : {r.get('used_model')}")
print(f"fallback    : {r.get('fallback_reason')}")
print(f"selected    : {r.get('selected_action')}")
print(f"choices     : {len(r.get('choices') or [])}")
for c in r.get("choices") or []:
    print(f"   {c.get('label')}  {c.get('variant_id')}")
print(f"reply       : {str(r.get('reply'))[:120]}")