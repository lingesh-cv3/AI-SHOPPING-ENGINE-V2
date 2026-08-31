from pathlib import Path

lines = Path("storefront/src/App.tsx").read_text(encoding="utf-8").split("\n")
for i in range(72, 105):
    print(f"{i + 1:4}  {lines[i]}")