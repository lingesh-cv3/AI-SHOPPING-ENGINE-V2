"""Count what actually happened, not just what was attempted.

A fuzzer that passes on its first run is either lucky or not reaching anything. Half
these actions can silently no-op - remove returns early if the proxy shape differs,
pay returns early on an empty cart - and if they did, the cart invariant held because
nothing changed the cart.

So the run reports how many times each action really took effect. Green with zero
removes and zero payments is worth less than a failure.
"""

from pathlib import Path

f = Path("fuzz.py")
s = f.read_text(encoding="utf-8")

if "COVERAGE" in s:
    print("already applied")
    raise SystemExit(0)

s = s.replace(
    "failures: list[tuple[int, str, list[str]]] = []",
    "#: What each action actually did, so a green run can be checked for reach.\n"
    "COVERAGE: dict[str, int] = {}\n"
    "\n"
    "failures: list[tuple[int, str, list[str]]] = []",
    1,
)

s = s.replace(
    '        log.append(f"{connection[-6:]}: {action(s, rng)}")',
    '        outcome = action(s, rng)\n'
    '        COVERAGE[outcome.split(" ->")[0].split(" (")[0]] = (\n'
    '            COVERAGE.get(outcome.split(" ->")[0].split(" (")[0], 0) + 1\n'
    "        )\n"
    '        log.append(f"{connection[-6:]}: {outcome}")',
    1,
)

s = s.replace(
    'print(f"  {checks_run} assertions across {args.sequences} sequences")',
    'print(f"  {checks_run} assertions across {args.sequences} sequences")\n'
    "print()\n"
    'print("  what actually happened:")\n'
    "for what, count in sorted(COVERAGE.items(), key=lambda kv: -kv[1]):\n"
    '    print(f"    {count:4}  {what}")',
    1,
)

f.write_text(s, encoding="utf-8", newline="\n")

if "COVERAGE" in f.read_text(encoding="utf-8"):
    print("applied")
else:
    print("FAILED")
    raise SystemExit(1)
