import asyncio


async def main():
    from engine.api.deps import engine

    r = await engine.reasoning.reason(
        friction=None,
        message="my card was declined",
        skip_model=True,
    )
    print("\n  used_model     :", r.used_model)
    print("  fallback_reason:", r.fallback_reason)
    print("  actions        :", [str(a.action_type) for a in r.actions])
    print()
    if r.used_model:
        print("  WRONG - it called the model despite skip_model")
    else:
        print("  ok - it answered without the model")


asyncio.run(main())