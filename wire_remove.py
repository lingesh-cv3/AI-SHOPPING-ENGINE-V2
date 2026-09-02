"""Pass onRemoveLine from App. Without it the button renders nothing, correctly.

CartPanel has the control and guards it with {onRemoveLine && ...}. App never passed
the handler, so the guard did its job and hid a button that had no way to work. The
patch that was meant to add this reported success on the panel half and I did not
check the other.
"""

from pathlib import Path

f = Path("storefront/src/App.tsx")
lines = f.read_text(encoding="utf-8").split("\n")

if any("onRemoveLine" in l for l in lines):
    print("already wired")
    raise SystemExit(0)

# Find where CartPanel is rendered.
start = None
for i, l in enumerate(lines):
    if "<CartPanel" in l:
        start = i
        break

if start is None:
    print("FAILED: could not find <CartPanel in App.tsx")
    raise SystemExit(1)

# Find its closing />.
close = start
while close < len(lines) and "/>" not in lines[close]:
    close += 1

if close >= len(lines):
    print("FAILED: could not find the end of the CartPanel element")
    raise SystemExit(1)

lines[close:close] = [
    "        onRemoveLine={async (lineId) => {",
    "          if (!cart) return;",
    "          // No model involved. Emptying your own basket should not depend on a",
    "          // provider being available, which until now it did - the only way out",
    "          // of a cart was to ask the assistant.",
    "          setCart(await api.changeLine(cart.cart_id, lineId, 0));",
    "        }}",
]

f.write_text("\n".join(lines), encoding="utf-8", newline="\n")

after = f.read_text(encoding="utf-8")
if "api.changeLine" in after and "onRemoveLine" in after:
    print(f"applied - inserted before line {close + 1}")
else:
    print("FAILED")
    raise SystemExit(1)
