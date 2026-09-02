"""Mint the keys this deployment needs, and print them once.

Run after stage 1, with the engine's database schema created:

    python mint_keys.py

It writes the keys it creates to .env.keys, which is gitignored, because they cannot
be recovered afterwards - only the hash is stored. Losing them means minting again.

Five keys, which is the smallest set that exercises every path:

  one publishable and one secret per merchant, and one operator key for CV3
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from engine.db import create_schema, dispose, keys


async def main() -> None:
    await create_schema()

    existing = await keys.list_keys()
    if existing:
        print()
        print(f"  {len(existing)} key(s) already exist:")
        for k in existing:
            state = " (revoked)" if k["revoked"] else ""
            print(f"    {k['prefix']}...  {k['kind']:12} {k['connection_id'] or 'all'}{state}")
        print()
        print("  Minting more would work, but you probably want the ones you have.")
        print("  Delete cv3.db to start over.")
        await dispose()
        return

    minted: list[tuple[str, str]] = []

    for cid, name in (("conn_demo", "Northfield"), ("conn_kettle", "Kettle & Bloom")):
        pk = await keys.mint(
            "publishable",
            connection_id=cid,
            label=f"{name} storefront",
            # Empty means any origin, which is right for local development and
            # wrong for production. A real deployment locks this to the merchant's
            # domain, which is most of what makes a browser key safe.
            allowed_origins=[],
        )
        sk = await keys.mint(
            "secret",
            connection_id=cid,
            label=f"{name} server",
        )
        minted.append((f"CV3_PUBLISHABLE_{cid.upper()}", pk))
        minted.append((f"CV3_SECRET_{cid.upper()}", sk))

    op = await keys.mint("operator", label="CV3 operations")
    minted.append(("CV3_OPERATOR_KEY", op))

    out = Path(".env.keys")
    out.write_text(
        "# Minted by mint_keys.py. Shown once - only hashes are stored.\n"
        "# Gitignored. If you lose these, mint new ones.\n\n"
        + "\n".join(f"{name}={value}" for name, value in minted)
        + "\n",
        encoding="utf-8",
    )

    print()
    print(f"  {len(minted)} keys minted and written to .env.keys")
    print()
    # The names only. The values are not printed here on purpose: they were, and
    # that is how all five ended up pasted into a chat log twice. The file is the
    # right place for key material; a terminal is a place people copy from without
    # thinking about what they are copying.
    for name, _ in minted:
        print(f"    {name}")
    print()
    print("  The keys themselves are in .env.keys and nowhere else.")
    print()
    print("  These are shown once. Only the hashes are in the database now.")
    print()
    print("  Add .env.keys to .gitignore if it is not already there.")
    print()

    await dispose()


asyncio.run(main())