from pathlib import Path

f = Path("storefront/src/MerchantConsole.tsx")
lines = f.read_text(encoding="utf-8").split("\n")

# Drop the temporary logging and the two comment lines above it.
out = []
skip_next = 0
for line in lines:
    if "merchant console refresh failed" in line:
        # Remove the two comment lines already appended, if they are there.
        while out and out[-1].strip().startswith("//"):
            out.pop()
        continue
    out.append(line)

f.write_text("\n".join(out), encoding="utf-8", newline="\n")
print("removed")