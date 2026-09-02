import asyncio


async def main():
    from shared.models import FrictionType
    from engine.api.deps import engine

    for label in ("conn_demo", "conn_kettle"):
        r = await engine.reasoning.reason(
            friction=FrictionType.PAYMENT_DECLINED,
            message="my card was declined",
            skip_model=True,
        )
        print(f"\n  {label}")
        print("    used_model:", r.used_model)
        print("    actions   :", [str(a.action_type) for a in r.actions])

    print()
    print("  Expect payment-recovery actions, not product recommendations.")
    print("  The capability check then drops whatever the platform cannot do,")
    print("  which is why the same list is right for both merchants here.")


asyncio.run(main())