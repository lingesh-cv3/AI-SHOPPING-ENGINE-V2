"""Create a shopper account that predates the email field, for the browser walk.

Signup now requires an email, but accounts created before the field existed have
email NULL - the checkout gate has to prompt for one. There is no API to make
one, so this signs up through the engine's real endpoint (email required) and
then nulls the email in the dev database, which is exactly how the legacy rows
got there in the first place.

Prints a JSON line: {"username": ..., "password": ...}
"""
import json
import http.cookiejar
import sqlite3
import sys
import urllib.error
import urllib.request
import uuid

ENGINE = "http://127.0.0.1:8000"
KEY = None
for line in open(".env.keys", encoding="utf-8").read().splitlines():
    s = line.strip()
    if s and not s.startswith("#") and "=" in s:
        n, _, v = s.partition("=")
        if n.strip() == "CV3_SECRET_CONN_DEMO":
            KEY = v.strip()

username = f"legacy_{uuid.uuid4().hex[:8]}"
password = "password123"

jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

body = json.dumps({
    "connection_id": "conn_demo",
    "username": username,
    "password": password,
    "email": f"{username}@example.com",
    "guest_session": None,
    "guest_cart": None,
}).encode()

req = urllib.request.Request(
    f"{ENGINE}/api/account/signup",
    data=body,
    method="POST",
    headers={"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"},
)
try:
    with opener.open(req, timeout=30) as r:
        resp = json.loads(r.read() or "{}")
except urllib.error.HTTPError as e:
    print(f"signup failed: HTTP {e.code} {e.read().decode()[:200]}", file=sys.stderr)
    sys.exit(1)

if not resp.get("username"):
    print(f"signup returned nothing usable: {resp}", file=sys.stderr)
    sys.exit(1)

conn = sqlite3.connect("cv3.db")
cur = conn.execute(
    "UPDATE shoppers SET email=NULL WHERE username=? AND connection_id='conn_demo'",
    (username,),
)
conn.commit()
if cur.rowcount != 1:
    print(f"did not find the account to legacy-ify: {username}", file=sys.stderr)
    sys.exit(1)
conn.close()

print(json.dumps({"username": username, "password": password}))