import json
import sqlite3

c = sqlite3.connect("cv3.db")

rows = list(
    c.execute(
        "select query, selected_action, proposed, shopper_reply "
        "from cases order by created_at desc limit 6"
    )
)

for q, action, proposed, reply in rows:
    print(f"\nshopper said : {q!r}")
    print(f"selected     : {action}")
    try:
        for p in json.loads(proposed):
            print(f"  proposed   : {p['action_type']}  params={p.get('parameters')}")
    except Exception:
        pass
    print(f"reply        : {(reply or '')[:70]}")