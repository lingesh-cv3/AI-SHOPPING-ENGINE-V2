from engine.api.main import app

print()
for route in app.routes:
    path = getattr(route, "path", None)
    if path is None:
        # An included router; walk into it.
        for inner in getattr(route, "routes", []) or []:
            p = getattr(inner, "path", "")
            if "lines" in p:
                methods = sorted(getattr(inner, "methods", []) or [])
                print(f"  {','.join(methods):10} {p}")
        continue
    if "lines" in path:
        methods = sorted(getattr(route, "methods", []) or [])
        print(f"  {','.join(methods):10} {path}")
print()