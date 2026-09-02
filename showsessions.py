import sqlite3

c = sqlite3.connect("cv3.db")

print("\nsessions the engine has turns for:")
rows = list(
    c.execute(
        "select session_id, connection_id, count(*), min(created_at) "
        "from session_turns group by session_id, connection_id "
        "order by min(created_at) desc limit 8"
    )
)
if not rows:
    print("  none")
for sid, conn, n, first in rows:
    print(f"  {sid:22} {conn:14} {n:3} turns   first {first}")