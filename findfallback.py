import inspect
from pathlib import Path

from engine.reasoning.service import ReasoningService

print("\nall methods, private included:")
for name, member in inspect.getmembers(ReasoningService, inspect.isfunction):
    print(f"  {name}{inspect.signature(member)}")

print("\nwhat the no-client branch does:")
lines = Path("engine/reasoning/service.py").read_text(encoding="utf-8").split("\n")
for i, l in enumerate(lines):
    if "if self._client is None:" in l:
        for j in range(i, min(i + 12, len(lines))):
            print(f"{j + 1:4}  {lines[j].rstrip()}")
        break