from pathlib import Path

text = Path("storefront/src/App.tsx").read_text(encoding="utf-8")
lines = text.split("\n")

for i, l in enumerate(lines):
    if "function sessionFor" in l:
        for j in range(i, min(i + 14, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")
        break

print()
for i, l in enumerate(lines):
    if "sessionFor(" in l and "function" not in l:
        print(f"{i + 1:4}  {lines[i].rstrip()}")