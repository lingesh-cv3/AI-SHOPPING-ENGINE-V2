"""Wire the remove control into the panel, by line.

The route and the api call went in; this did not, because my patch looked for a
<div> where the line total is a <span>. So a shopper still could not take anything
out of their basket without the model.
"""

from pathlib import Path

problems = []

# ---------------------------------------------------------------------------
# 1. The panel takes the handler.
# ---------------------------------------------------------------------------

panel = Path("storefront/src/CartPanel.tsx")
lines = panel.read_text(encoding="utf-8").split("\n")

if any("onRemoveLine" in l for l in lines):
    print("  ok      panel already has it")
else:
    # The line total is line 87 (index 86). Replace it with the total plus a button.
    i = 86
    if lines[i].strip() != '<span className="num">{line.line_total.display}</span>':
        problems.append(f"line 87 is not the line total: {lines[i].strip()[:60]}")
    else:
        lines[i] = (
            '                <span className="line-right">\n'
            '                  <span className="num">{line.line_total.display}</span>\n'
            "                  {onRemoveLine && (\n"
            "                    <button\n"
            '                      className="line-remove"\n'
            "                      aria-label={`Remove ${line.title}`}\n"
            '                      title="Remove"\n'
            "                      onClick={() => onRemoveLine(line.line_id)}\n"
            "                    >\n"
            "                      &times;\n"
            "                    </button>\n"
            "                  )}\n"
            "                </span>"
        )

        # The prop, in the destructuring at line 47 (index 46) and the type at 55.
        lines[46] = "  onCheckout,\n  onRemoveLine,"
        for j, l in enumerate(lines):
            if l.strip() == "onCheckout: (cardLast4: string) => void;":
                lines[j] = (
                    "  onCheckout: (cardLast4: string) => void;\n"
                    "  /** Take a line out. Undefined hides the control, so the panel\n"
                    "   *  still works anywhere it is rendered without one. */\n"
                    "  onRemoveLine?: (lineId: string) => void;"
                )
                break

        panel.write_text("\n".join(lines), encoding="utf-8", newline="\n")
        print("  ok      panel control added")

# ---------------------------------------------------------------------------
# 2. App passes it.
# ---------------------------------------------------------------------------

app = Path("storefront/src/App.tsx")
a = app.read_text(encoding="utf-8")

if "onRemoveLine" in a:
    print("  ok      App already wires it")
else:
    old = "          couponHint={merchant.couponHint}"
    if old not in a:
        problems.append("could not find couponHint in the CartPanel props")
    else:
        a = a.replace(
            old,
            old
            + "\n"
            + "          onRemoveLine={async (lineId) => {\n"
            + "            if (!cart) return;\n"
            + "            // No model involved. Emptying your own basket should not\n"
            + "            // depend on a provider being available, which until now it\n"
            + "            // did - the only way out of a cart was to ask.\n"
            + "            setCart(await api.changeLine(cart.cart_id, lineId, 0));\n"
            + "          }}",
            1,
        )
        app.write_text(a, encoding="utf-8", newline="\n")
        print("  ok      App wires it")

print()
if problems:
    for p in problems:
        print(f"  FAILED  {p}")
    raise SystemExit(1)

# Verified rather than assumed.
missing = [
    n
    for n, path, marker in (
        ("panel prop", "storefront/src/CartPanel.tsx", "onRemoveLine?:"),
        ("panel button", "storefront/src/CartPanel.tsx", "line-remove"),
        ("App wiring", "storefront/src/App.tsx", "api.changeLine"),
    )
    if marker not in Path(path).read_text(encoding="utf-8")
]

if missing:
    print("  did not land:")
    for m in missing:
        print(f"    {m}")
    raise SystemExit(1)

print("  All three landed.")
print()
print("  cd storefront ; npx tsc --noEmit ; cd ..")