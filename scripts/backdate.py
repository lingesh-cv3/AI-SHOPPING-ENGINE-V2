import sqlite3
from datetime import UTC, datetime, timedelta

c = sqlite3.connect("cv3.db")
past = (datetime.now(UTC) - timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S.%f")
c.execute("update approvals set expires_at=? where state='PENDING'", (past,))
c.commit()
print("backdated", c.total_changes, "approval(s) to", past)
