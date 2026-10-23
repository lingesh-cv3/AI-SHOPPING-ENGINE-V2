"""`accounts._finish` cart decisions with a controlled platform double (in-process, isolated database).

The HTTP journey is in test_cart_persistence_signin.py; this pins the decisions that need a platform that
fails on demand:

  empty guest cart + remembered cart exists            -> remembered kept (the user-reported bug)
  guest cart holds a line                              -> guest wins (a fresh choice is never discarded)
  guest cart unreadable (transient platform error)     -> remembered kept (unknown is not "empty")
  remembered cart unreadable (transient)               -> remembered kept (never treated as "gone")
  remembered cart definitively gone (CART_NOT_FOUND)   -> the guest cart wins
  remembered cart invalid (CART_INVALID)               -> the guest cart wins
  guest cart not held by the caller                    -> never adopted; the remembered cart is returned
  no remembered cart                                   -> the guest cart (unchanged)

    python tests/shopper_v2/test_cart_restore_finish_unit.py        (no services needed)
"""
import os
import sys

# FIRST: an isolated database, before anything from `engine` is imported (a stray import binds cv3.db).
_DB = os.path.join(os.path.dirname(__file__), "..", "..", "_test_cart_restore_finish.db")
if os.path.exists(_DB):
    os.remove(_DB)
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./_test_cart_restore_finish.db"
os.environ["OPENAI_API_KEY"] = ""
os.environ["GROQ_API_KEY"] = ""
os.environ["MAILER_SMTP_HOST"] = ""
assert "engine" not in sys.modules and "cv3.db" not in os.environ["DATABASE_URL"]
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import asyncio  # noqa: E402
import uuid  # noqa: E402

failures = 0


def check(label, ok, got=None):
    global failures
    print(f"  {'ok    ' if ok else 'FAILED'}  {label}" + (f"   got: {got}" if (not ok and got is not None) else ""))
    if not ok:
        failures += 1


class _Line:
    pass


class _Cart:
    def __init__(self, n):
        self.lines = [_Line() for _ in range(n)]


class Platform:
    """carts: {cart_id: n_lines | Exception}"""

    def __init__(self, carts):
        self.carts = carts

    async def get_cart(self, cart_id):
        v = self.carts.get(cart_id)
        if isinstance(v, Exception):
            raise v
        if v is None:
            from shared.models.errors import CommerceError, ErrorCode

            raise CommerceError(ErrorCode.CART_NOT_FOUND, "no such cart")
        return _Cart(v)


class Registry:
    def __init__(self, platform):
        self.platform = platform

    def adapter_for(self, connection_id):
        return self.platform


class FakeEngine:
    def __init__(self, platform):
        self.registry = Registry(platform)


class Who:
    """A visitor holding exactly the given keys."""

    def __init__(self, keys):
        self._keys = keys

    async def keys_for(self, connection_id):
        return list(self._keys)


async def run_case(label, carts, *, remembered, guest, guest_held=True, expect, why):
    from engine import db
    from engine.api import accounts, deps
    from shared.models.errors import CommerceError, ErrorCode  # noqa: F401

    deps.engine = FakeEngine(Platform(carts))  # `_remembered_cart_wins` imports `engine` from deps at call time
    conn = "conn_demo"
    sid = f"shp_{uuid.uuid4().hex[:10]}"
    shopper = {"shopper_id": sid}
    visitor_key = f"vk_{uuid.uuid4().hex[:8]}"
    if remembered:
        await db.owners.claim_safely(conn, db.owners.CART, remembered, sid)
        await db.shopper_carts.remember(sid, conn, remembered)
    if guest:
        if guest_held:
            await db.owners.claim_safely(conn, db.owners.CART, guest, visitor_key)
        else:
            await db.owners.claim_safely(conn, db.owners.CART, guest, f"someone_else_{uuid.uuid4().hex[:6]}")
    out = await accounts._finish(shopper, conn, None, guest, Who([visitor_key]))
    now = await db.shopper_carts.remembered(sid, conn)
    check(f"{label}: returns {expect!r} ({why})", out["cart_id"] == expect, out["cart_id"])
    check(f"{label}: the account's remembered cart agrees with what was returned", now == out["cart_id"], now)


async def main():
    from shared.models.errors import CommerceError, ErrorCode
    from engine.db import create_schema, dispose
    import engine.db  # noqa: F401

    await create_schema()
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    print("1. The reported bug and its boundaries")
    await run_case("empty guest + remembered exists", {R: 1, G: 0}, remembered=R, guest=G, expect=R,
                   why="the cart that holds the product is restored")
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    await run_case("guest holds a line", {R: 1, G: 2}, remembered=R, guest=G, expect=G,
                   why="a fresh choice is never discarded")
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    await run_case("both empty", {R: 0, G: 0}, remembered=R, guest=G, expect=R, why="nothing to protect either way; remembered kept")

    print("\n2. Platform trouble is never read as 'gone'")
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    await run_case("guest unreadable (timeout)", {R: 1, G: CommerceError(ErrorCode.TIMEOUT, "slow")}, remembered=R, guest=G, expect=R,
                   why="unknown is not empty")
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    await run_case("remembered unreadable (upstream error)", {R: CommerceError(ErrorCode.UPSTREAM_ERROR, "boom"), G: 0},
                   remembered=R, guest=G, expect=R, why="a transient error must not discard a valid persisted cart")
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    await run_case("remembered unreadable (plain exception)", {R: RuntimeError("socket"), G: 0},
                   remembered=R, guest=G, expect=R, why="any non-definitive failure keeps the persisted cart")

    print("\n3. Expired / invalid cart identifiers")
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    await run_case("remembered cart gone (CART_NOT_FOUND)", {G: 0}, remembered=R, guest=G, expect=G, why="the platform says it is gone")
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    await run_case("remembered cart invalid (CART_INVALID)", {R: CommerceError(ErrorCode.CART_INVALID, "expired"), G: 0},
                   remembered=R, guest=G, expect=G, why="expired/invalid is definitive")

    print("\n4. Ownership and first-time shoppers")
    R, G = f"R{uuid.uuid4().hex[:5]}", f"G{uuid.uuid4().hex[:5]}"
    await run_case("guest cart NOT held by the caller", {R: 1, G: 3}, remembered=R, guest=G, guest_held=False, expect=R,
                   why="a cart merely named is never adopted")
    G = f"G{uuid.uuid4().hex[:5]}"
    await run_case("no remembered cart, guest held", {G: 0}, remembered=None, guest=G, expect=G, why="the first cart becomes theirs")
    G = f"G{uuid.uuid4().hex[:5]}"
    await run_case("no remembered cart, guest NOT held", {G: 3}, remembered=None, guest=G, guest_held=False, expect=None,
                   why="nothing to restore and nothing adoptable")

    await dispose()
    print(f"\n  {'ALL CHECKS PASSED' if failures == 0 else f'{failures} CHECK(S) FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
