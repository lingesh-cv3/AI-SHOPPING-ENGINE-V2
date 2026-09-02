import json
import sqlite3

c = sqlite3.connect("cv3.db")

for q, action, proposed, used, friction in c.execute(
    "select query, selected_action, proposed, used_model, friction_type "
    "from cases order by created_at desc limit 3"
):
    print(f"\nsaid       : {q!r}")
    print(f"friction   : {friction}")
    print(f"selected   : {action}")
    print(f"used_model : {bool(used)}")
    try:
        for p in json.loads(proposed):
            print(f"  proposed {p['action_type']}")
    except Exception:
        pass