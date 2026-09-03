"""Signing shoppers in and out, and slowing down anyone guessing.

Sessions live in the database rather than in a token, so signing out can actually
end one. Everything here is about that being true.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from .models import ShopperSession, SignInAttempt
from .session import session_scope

#: How long a session lasts without being used.
#:
#: Thirty days is a shopping site, not a bank. Someone who bought coffee last month
#: should not have to sign in again to ask where it went, and the thing being
#: protected is a conversation history and an order list rather than a balance.
SESSION_DAYS = 30

#: Failures allowed before a username is refused, and the window they count in.
#:
#: Five in fifteen minutes. Enough that somebody mistyping twice and then checking
#: their password manager is unaffected, and few enough that guessing at any useful
#: rate stops.
MAX_FAILURES = 5
FAILURE_WINDOW = timedelta(minutes=15)


def _hash(token: str) -> str:
    """SHA-256, and that is deliberate.

    Same reasoning as the API keys and the opposite of the one for passwords: a
    token is 32 bytes of urandom, so there is no dictionary to attack and nothing
    to slow down. A fast hash keeps verification cheap on a path every request
    takes.
    """
    return hashlib.sha256(token.encode()).hexdigest()


async def locked_out(connection_id: str, username: str) -> bool:
    """Whether this username has failed too often lately.

    By username rather than by IP. An IP is shared by everyone behind a router and
    trivially changed by anyone who cares, so limiting on it punishes an office and
    stops nobody. Limiting the account being guessed at is the thing that matches
    the attack.

    The trade is that somebody could lock a known username out on purpose. Fifteen
    minutes is short enough that it is an annoyance rather than a denial, and the
    alternative - letting guessing run unlimited - is worse.
    """
    since = datetime.now(UTC) - FAILURE_WINDOW

    async with session_scope() as db:
        result = await db.execute(
            select(func.count())
            .select_from(SignInAttempt)
            .where(
                SignInAttempt.connection_id == connection_id,
                SignInAttempt.username == username.strip(),
                SignInAttempt.at >= since,
            )
        )
        return (result.scalar() or 0) >= MAX_FAILURES


async def record_failure(connection_id: str, username: str) -> None:
    """Note a failed sign-in, and clear out old ones.

    Old rows are deleted here rather than by a sweep, because this is the only
    place that cares and a table nobody prunes grows forever.
    """
    now = datetime.now(UTC)

    async with session_scope() as db:
        db.add(
            SignInAttempt(
                attempt_id=f"att_{uuid.uuid4().hex[:16]}",
                connection_id=connection_id,
                username=username.strip(),
                at=now,
            )
        )
        await db.execute(
            delete(SignInAttempt).where(SignInAttempt.at < now - FAILURE_WINDOW * 4)
        )


async def clear_failures(connection_id: str, username: str) -> None:
    """Forget failures after a successful sign-in.

    Otherwise somebody who mistyped four times and then got it right stays one
    mistake away from being locked out, which is a strange way to treat a person
    who has just proved who they are.
    """
    async with session_scope() as db:
        await db.execute(
            delete(SignInAttempt).where(
                SignInAttempt.connection_id == connection_id,
                SignInAttempt.username == username.strip(),
            )
        )


async def start(shopper_id: str, connection_id: str) -> str:
    """A new session. Returns the token once.

    Always a fresh token, never reusing or extending an existing one. A token the
    caller supplied or that was already sitting in a cookie could have been planted
    there, and then whoever planted it is signed in as this shopper.
    """
    token = f"shs_{os.urandom(32).hex()}"

    async with session_scope() as db:
        db.add(
            ShopperSession(
                session_token_id=f"ses_{uuid.uuid4().hex[:16]}",
                prefix=token[:12],
                token_hash=_hash(token),
                shopper_id=shopper_id,
                connection_id=connection_id,
                expires_at=datetime.now(UTC) + timedelta(days=SESSION_DAYS),
            )
        )

    return token


async def resolve(token: str | None, connection_id: str) -> dict | None:
    """Who this token belongs to, or None.

    Checks the merchant too. A session started at Kettle must not identify anybody
    at Northfield - the accounts are separate, so the sessions have to be.

    Returns None for expired and revoked alike, rather than distinguishing them.
    """
    if not token or len(token) < 12:
        return None

    now = datetime.now(UTC)
    wanted = _hash(token)

    async with session_scope() as db:
        result = await db.execute(
            select(ShopperSession).where(ShopperSession.prefix == token[:12])
        )

        for row in result.scalars():
            if row.token_hash != wanted:
                continue
            if row.revoked_at is not None:
                return None
            if row.connection_id != connection_id:
                return None

            expires = row.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=UTC)
            if expires <= now:
                return None

            row.last_used_at = now
            return {
                "shopper_id": row.shopper_id,
                "connection_id": row.connection_id,
            }

    return None


async def end(token: str | None) -> bool:
    """Sign out. Returns whether a session was ended.

    Revoked rather than deleted, so the record of when somebody signed out
    survives. And this is the whole reason sessions are server-side: with a JWT
    this function could only hope the browser threw the token away.
    """
    if not token or len(token) < 12:
        return False

    wanted = _hash(token)

    async with session_scope() as db:
        result = await db.execute(
            select(ShopperSession).where(ShopperSession.prefix == token[:12])
        )
        for row in result.scalars():
            if row.token_hash == wanted and row.revoked_at is None:
                row.revoked_at = datetime.now(UTC)
                return True

    return False


async def end_all(shopper_id: str) -> int:
    """Sign out everywhere. Returns how many sessions ended.

    For "sign me out of all devices", and for the moment a password changes - at
    which point every existing session should stop, because the reason somebody
    changes a password is usually that they think someone else has it.
    """
    now = datetime.now(UTC)
    ended = 0

    async with session_scope() as db:
        result = await db.execute(
            select(ShopperSession).where(
                ShopperSession.shopper_id == shopper_id,
                ShopperSession.revoked_at.is_(None),
            )
        )
        for row in result.scalars():
            row.revoked_at = now
            ended += 1

    return ended