"""Prove the account functions work before anything depends on them.

Checked against the real thing rather than by grepping the file, because three
patches this week reported success while doing nothing.

    python testshopper.py
"""

import asyncio


async def main() -> None:
    from engine.db import create_schema, dispose, shoppers
    from engine.db.shoppers import SignUpError

    await create_schema()

    print()

    # A new account.
    try:
        made = await shoppers.create("conn_kettle", "lingesh", "coffee-and-shoes")
        print(f"  ok      created {made['username']} as {made['shopper_id']}")
    except SignUpError as e:
        print(f"  note    already exists or refused: {e}")

    # The right password.
    who = await shoppers.verify("conn_kettle", "lingesh", "coffee-and-shoes")
    print(f"  {'ok    ' if who else 'FAILED'}  correct password accepted")

    # The wrong one.
    nope = await shoppers.verify("conn_kettle", "lingesh", "wrong-password")
    print(f"  {'ok    ' if nope is None else 'FAILED'}  wrong password refused")

    # An account that does not exist.
    ghost = await shoppers.verify("conn_kettle", "nobody", "whatever")
    print(f"  {'ok    ' if ghost is None else 'FAILED'}  unknown username refused")

    # The same username at the other merchant is a different person.
    #
    # This is the isolation that matters: an account at Kettle is not an account at
    # Northfield, so the same name and password must not work across both.
    other = await shoppers.verify("conn_demo", "lingesh", "coffee-and-shoes")
    print(
        f"  {'ok    ' if other is None else 'FAILED'}  "
        "the same account does not work at the other merchant"
    )

    # And it can be created there separately.
    try:
        await shoppers.create("conn_demo", "lingesh", "different-password")
        print("  ok      the same username can exist at both shops, separately")
    except SignUpError as e:
        print(f"  note    {e}")

    # A short password.
    try:
        await shoppers.create("conn_kettle", "someone", "short")
        print("  FAILED  a five-character password was accepted")
    except SignUpError:
        print("  ok      a short password is refused")

    # A silly username.
    try:
        await shoppers.create("conn_kettle", "a b c", "long-enough-password")
        print("  FAILED  a username with spaces was accepted")
    except SignUpError:
        print("  ok      a username with spaces is refused")

    print()
    print("  If every line reads ok, identity works and stage 2 can build on it.")
    print()

    await dispose()


asyncio.run(main())