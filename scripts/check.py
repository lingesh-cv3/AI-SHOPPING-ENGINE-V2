import json
import sqlite3

c = sqlite3.connect("cv3.db")
row = c.execute(
    "select proposed, query from cases "
    "where selected_action='APPLY_PROMOTION' "
    "order by created_at desc limit 1"
).fetchone()

if row is None:
    print("no APPLY_PROMOTION case found")
else:
    print("query:", row[1])
    print(json.dumps(json.loads(row[0]), indent=2))