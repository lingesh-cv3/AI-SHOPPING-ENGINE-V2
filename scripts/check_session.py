import sqlite3

c = sqlite3.connect("cv3.db")
sid = "sess_a9wnz1cy3c"

rows = list(
    c.execute(
        "select connection_id, speaker, substr(text,1,40) "
        "from session_turns where session_id=?",
        (sid,),
    )
)

print(len(rows), "turns for", sid)
for r in rows:
    print("  ", r)