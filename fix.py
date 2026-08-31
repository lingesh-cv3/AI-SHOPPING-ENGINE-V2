"""Remove the orphaned lines left by patch16.

The replacement cut the initialiser at the first ');' it found, which was inside the
function body rather than at the end of the useState call. Lines 93 to 98 are the
tail of the old initialiser with nothing above them.
"""

from pathlib import Path

f = Path("storefront/src/App.tsx")
lines = f.read_text(encoding="utf-8").split("\n")

# Verify before cutting. Line 93 is index 92.
if "if (existing) return existing;" not in lines[92]:
    print("FAILED: line 93 is not what was expected. It says:")
    print(f"  {lines[92]}")
    raise SystemExit(1)

if lines[97].strip() != ");":
    print("FAILED: line 98 is not the closing paren. It says:")
    print(f"  {lines[97]}")
    raise SystemExit(1)

# Drop indices 92 through 97, which is lines 93 to 98.
out = lines[:92] + lines[98:]
f.write_text("\n".join(out), encoding="utf-8", newline="\n")
print(f"removed 6 orphaned lines; file now {len(out)} lines")