import inspect

from engine.reasoning.service import ReasoningService

print("\npublic methods on ReasoningService:")
for name, member in inspect.getmembers(ReasoningService, inspect.isfunction):
    if name.startswith("_"):
        continue
    print(f"\n  {name}{inspect.signature(member)}")