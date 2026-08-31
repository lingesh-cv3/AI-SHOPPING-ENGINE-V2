"""Find out which link in the chain is broken.

Three things have to line up for a conversation to survive a reload: the browser
keeps the session id, the engine stores turns under that id, and the widget asks for
them. This checks the middle one and tells you which side to look at.
"""

import json
import sqlite3
import urllib.request

c = sqlite3.connect("cv3.db")

rows = list(
    c.execute(
        "select session_id, connection_id, count(*) "
        "from session_turns group by session_id, connection_id "
        "order by max(created_at) desc limit 5"
    )
)

print()
if not rows:
    print("  The engine has NO turns stored at all.")
    print("  So nothing is being written. The problem is the backend.")
else:
    print("  Sessions the engine knows about, newest first:")
    for sid, conn, n in rows:
        print(f"    {sid}   {conn}   {n} turns")

    newest = rows[0][0]
    conn = rows[0][1]
    print()
    print(f"  Asking the engine for {newest} on {conn}:")
    url = f"http://127.0.0.1:8000/api/chat/{conn}/{newest}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            d = json.loads(r.read())
        print(f"    the endpoint returns {len(d['turns'])} turns")
        for t in d["turns"][:4]:
            print(f"      {t['speaker']}: {t['text'][:50]}")
    except Exception as e:
        print(f"    the endpoint FAILED: {e}")

print()
print("  Now compare: in the browser console type")
print('    sessionStorage.getItem("cv3_session")')
print("  If that id is not in the list above, the browser and the engine")
print("  are using different sessions and that is the whole problem.")
print()