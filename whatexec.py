import sqlite3

c = sqlite3.connect("cv3.db")

cols = [r[1] for r in c.execute("pragma table_info(execution_attempts)")]
print("\ncolumns:", cols)

print("\nlast few attempts:")
rows = list(
    c.execute(
        "select " + ", ".join(cols) + " from execution_attempts "
        "order by rowid desc limit 4"
    )
)
if not rows:
    print("  none - execution was never attempted")
for row in rows:
    print()
    for name, value in zip(cols, row):
        text = str(value)
        print(f"  {name:22} {text[:90]}")