"""Add the note field to HandoverDone.

The earlier patch matched on the whole class including its docstring, and the
docstring had been written differently, so the replacement did nothing while the
route was updated to read body.note. Hence the 500.

Done by line here, and verified afterwards.
"""

from pathlib import Path

f = Path("engine/api/schemas.py")
lines = f.read_text(encoding="utf-8").split("\n")

i = 135  # line 136, the handled_by line
if lines[i].strip() != "handled_by: str":
    print("FAILED: line 136 is not handled_by. It says:")
    print(f"  {lines[i]}")
    raise SystemExit(1)

lines[i] = (
    "    handled_by: str\n"
    "\n"
    "    #: What the operator did, in their words, sent to the shopper verbatim.\n"
    "    #:\n"
    "    #: Optional, though the interface asks for it. Making it mandatory would\n"
    "    #: mean somebody in a hurry typing a full stop, which is worse than an\n"
    "    #: honest blank - a handover closed without a note still resolves, and the\n"
    "    #: shopper simply hears nothing.\n"
    "    note: str | None = None"
)

f.write_text("\n".join(lines), encoding="utf-8", newline="\n")

# Verified rather than assumed.
after = f.read_text(encoding="utf-8")
if "note: str | None = None" in after:
    print("applied")
else:
    print("FAILED: the field is still not there")
    raise SystemExit(1)