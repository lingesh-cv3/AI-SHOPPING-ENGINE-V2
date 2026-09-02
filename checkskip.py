import inspect
from pathlib import Path

from engine.reasoning.service import ReasoningService

sig = inspect.signature(ReasoningService.diagnose)
print("\ndiagnose parameters:")
for name, p in sig.parameters.items():
    print(f"  {name}")

print()
if "skip_model" in sig.parameters:
    print("  ok - diagnose accepts skip_model")
else:
    print("  MISSING - diagnose does not accept skip_model, so the flag is ignored")
    print("  and a declined payment will still wait on the provider.")

# And confirm it is actually used, not just accepted.
src = Path("engine/reasoning/service.py").read_text(encoding="utf-8")
if "skip_model" in src:
    uses = [l.strip() for l in src.split("\n") if "skip_model" in l]
    print("\n  references in service.py:")
    for u in uses:
        print(f"    {u}")
else:
    print("\n  and it appears nowhere in service.py")