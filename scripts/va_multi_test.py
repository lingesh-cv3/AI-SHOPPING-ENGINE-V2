"""Adversarial multi-item drop test. Scratch, read-only against running services."""
import http.cookiejar
import json
import urllib.request
import uuid

PK = "cv3_pk_cc3c132747a6cfa33638148ba3b17a9cc7e59db658b85ac87d5762eda6d9ce19"
BASE = "http://127.0.0.1:8000"


def make_opener():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))


def post(opener, path, body):
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {PK}"},
        method="POST",
    )
    with opener.open(req, timeout=90) as r:
        return json.loads(r.read())


def run(label, msg):
    opener = make_opener()
    sid = f"va-multi-{uuid.uuid4().hex[:8]}"
    cart = post(opener, "/api/shop/conn_demo/cart", {})
    cart_id = cart["cart_id"]
    body = {"connection_id": "conn_demo", "session_id": sid, "message": msg, "cart_id": cart_id}
    resp = post(opener, "/api/chat", body)
    print(f"=== {label} ===")
    print("msg:", msg)
    print("reply:", resp.get("reply"))
    print("choices:", resp.get("choices"))
    print("action_type/executed keys:", {k: v for k, v in resp.items() if k not in ("reply",)})
    print()


trials = [
    ("3-item mixed categories", "Add the Trailblazer Running Shoe size 8, the Merino Base Layer Tee size M, and the Energy Gel Box of 12"),
    ("3-item same category (footwear)", "Add the Trailblazer Running Shoe size 8, the Marathon Pro Racing Shoe size 8, and the Fell Runner Trail Shoe size 8"),
    ("4-item same category (footwear)", "Add the Trailblazer Running Shoe size 8, the Marathon Pro Racing Shoe size 8, the Fell Runner Trail Shoe size 8, and the Winter Road Shoe size 8"),
    ("4-item mixed w/ 3 footwear", "I want the Everyday Canvas Sneaker size 9, the Court Classic Tennis Shoe size 9, the Studio Training Flat size 9, and the Recovery Slide size 9"),
    ("4-item mixed categories", "Add the GPS Watch Series 4, the Heart Rate Chest Strap, the Foot Pod Cadence Sensor, and the Smart Scale"),
]

for label, msg in trials:
    try:
        run(label, msg)
    except Exception as e:
        print(f"=== {label} === ERROR: {e}\n")
