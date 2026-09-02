from pathlib import Path

lines = Path("engine/api/shop.py").read_text(encoding="utf-8").split("\n")

for i, l in enumerate(lines):
    if "/cart/{cart_id}/lines" in l:
        for j in range(i, min(i + 22, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")
        print()