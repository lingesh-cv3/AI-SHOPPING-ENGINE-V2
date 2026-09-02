from pathlib import Path

lines = Path("storefront/src/CartPanel.tsx").read_text(encoding="utf-8").split("\n")

print("\n--- the signature ---")
for i, l in enumerate(lines):
    if "export function CartPanel" in l:
        for j in range(i, min(i + 16, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")
        break

print("\n--- where a line is rendered ---")
for i, l in enumerate(lines):
    if "line_total" in l:
        for j in range(max(0, i - 12), min(i + 5, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")
        break

print("\n--- where App renders CartPanel ---")
app = Path("storefront/src/App.tsx").read_text(encoding="utf-8").split("\n")
for i, l in enumerate(app):
    if "<CartPanel" in l:
        for j in range(i, min(i + 12, len(app))):
            print(f"{j + 1:4}  {app[j].rstrip()}")
        break