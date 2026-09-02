import sqlite3

c = sqlite3.connect("cv3.db")

print("\nrecent cases:")
for cid, action, state, risk in c.execute(
    "select case_id, selected_action, state, risk_outcome "
    "from cases order by created_at desc limit 5"
):
    print(f"  {action:24} state={state:18} risk={risk}")

print("\napprovals waiting:")
rows = list(
    c.execute("select action_type, state from approvals where state='PENDING'")
)
print(f"  {len(rows)} pending")
for a, st in rows:
    print(f"    {a} {st}")

print("\nescalated cases with no approval row:")
orphans = list(
    c.execute(
        "select case_id, selected_action from cases "
        "where state='ESCALATED' and case_id not in (select case_id from approvals)"
    )
)
print(f"  {len(orphans)}")
for cid, action in orphans:
    print(f"    {cid} {action}")