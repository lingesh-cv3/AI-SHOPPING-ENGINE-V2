import json
import sqlite3

c = sqlite3.connect("cv3.db")

for q, action, proposed, rejected, used in c.execute(
    "select query, selected_action, proposed, rejected, used_model "
    "from cases order by created_at desc limit 3"
):
    print(f"\nsaid       : {q!r}")
    print(f"selected   : {action}")
    print(f"used_model : {bool(used)}")
    try:
        for p in json.loads(proposed):
            print(f"  proposed  {p['action_type']}")
    except Exception:
        pass
    try:
        for r in json.loads(rejected or "[]"):
            print(f"  REJECTED  {r.get('action_type')} - {r.get('reason')} - {r.get('detail')}")
    except Exception:
        pass