from pathlib import Path

lines = Path("engine/api/schemas.py").read_text(encoding="utf-8").split("\n")

hits = [i for i, l in enumerate(lines) if "class HandoverDone" in l]
print(f"\nHandoverDone defined {len(hits)} time(s)")

for i in hits:
    print()
    for j in range(i, min(i + 16, len(lines))):
        print(f"{j + 1:4}  {lines[j].rstrip()}")