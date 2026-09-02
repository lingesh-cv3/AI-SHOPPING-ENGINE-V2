import json
import sqlite3

c = sqlite3.connect("cv3.db")

for q, action, proposed, used in c.execute(
    "select query, selected_action, proposed, used_model "
    "from cases order by created_at desc limit 4"
):
    print(f"\nsaid       : {q!r}")
    print(f"selected   : {action}")
    print(f"used_model : {bool(used)}")
    try:
        for p in json.loads(proposed):
            print(f"  proposed {p['action_type']}  {p.get('parameters')}")
    except Exception:
        pass