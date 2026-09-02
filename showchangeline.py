from pathlib import Path

lines = Path("engine/api/shop.py").read_text(encoding="utf-8").split("\n")

for i, l in enumerate(lines):
    if "change_line" in l or "class ChangeLine" in l:
        print()
        for j in range(max(0, i - 4), min(i + 18, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")