"""Creating and verifying shopper accounts.

Separate from the repository because this is the only place that touches a password,
and that is worth being able to point at.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime

import bcrypt
from sqlalchemy import select

from .models import Shopper
from .session import session_scope

#: Deliberately permissive. A username is a handle, not an email address, and
#: refusing somebody's chosen name over a hyphen is friction for no benefit.
USERNAME = re.compile(r"^[A-Za-z0-9._@+-]{3,80}$")

#: Eight characters, and nothing else.
#:
#: No uppercase-plus-digit-plus-symbol rule, because those produce Password1! and
#: measurably worse passwords than a length floor does. Current NIST guidance says
#: length beats decoration.
MIN_PASSWORD = 8

#: Permissive on purpose. The confirmation email has to be deliverable, not
#: perfect - refusing an address over an exotic TLD is friction for no benefit.
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")


class SignUpError(Exception):
    """Why a sign-up was refused, in words worth showing somebody."""


def _hash(password: str) -> str:
    """A bcrypt hash, salt and cost included.

    The cost is chosen by the library and rises over time as hardware does, which
    is the point of using one rather than rolling this.
    """
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _matches(password: str, stored: str) -> bool:
    """Whether a password matches its hash.

    bcrypt's own comparison, which is constant-time. A plain == would leak how much
    of the hash matched through how long it took.
    """
    try:
        return bcrypt.checkpw(password.encode(), stored.encode())
    except (ValueError, TypeError):
        # A malformed stored hash is not a match. Refusing is the safe direction.
        return False


def _validate_email(email: str | None) -> str | None:
    """The email, trimmed, or None if it is absent.

    Raises SignUpError with a reason worth showing when it is present but not an
    address. A shopper who typo'd their email deserves to know, and the field only
    earns its keep if the confirmation actually arrives.
    """
    if not email:
        return None
    email = email.strip()
    if not EMAIL.match(email):
        raise SignUpError("That does not look like an email address.")
    return email


async def create(
    connection_id: str,
    username: str,
    password: str,
    *,
    display_name: str | None = None,
    email: str | None = None,
) -> dict:
    """A new shopper at one merchant. Raises SignUpError with a reason."""
    username = username.strip()
    email = _validate_email(email)

    if not USERNAME.match(username):
        raise SignUpError(
            "A username needs three to eighty characters, and can use letters, "
            "numbers, dots, dashes and @."
        )

    if len(password) < MIN_PASSWORD:
        raise SignUpError(
            f"A password needs at least {MIN_PASSWORD} characters. Longer and "
            "memorable beats short and complicated."
        )

    async with session_scope() as db:
        existing = await db.execute(
            select(Shopper).where(
                Shopper.connection_id == connection_id,
                Shopper.username == username,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise SignUpError("That username is taken at this shop.")

        shopper_id = f"shp_{uuid.uuid4().hex[:16]}"
        db.add(
            Shopper(
                shopper_id=shopper_id,
                connection_id=connection_id,
                username=username,
                password_hash=_hash(password),
                display_name=display_name or None,
                email=email,
            )
        )

    return {
        "shopper_id": shopper_id,
        "username": username,
        "display_name": display_name or username,
        "email": email,
    }


async def verify(connection_id: str, username: str, password: str) -> dict | None:
    """The shopper, or None.

    One answer for a wrong username and a wrong password, because distinguishing
    them tells somebody which half of their guess was right - and lets them find
    out who shops here.
    """
    async with session_scope() as db:
        result = await db.execute(
            select(Shopper).where(
                Shopper.connection_id == connection_id,
                Shopper.username == username.strip(),
            )
        )
        shopper = result.scalar_one_or_none()

        if shopper is None:
            # Hashed anyway, against nothing.
            #
            # Returning immediately would make an unknown username measurably
            # faster than a known one with a wrong password, which is how a list of
            # a shop's customers gets built.
            _matches(password, "$2b$12$" + "x" * 53)
            return None

        if not _matches(password, shopper.password_hash):
            return None

        shopper.last_seen_at = datetime.now(UTC)

        return {
            "shopper_id": shopper.shopper_id,
            "username": shopper.username,
            "display_name": shopper.display_name or shopper.username,
            "email": shopper.email,
        }


async def by_id(shopper_id: str) -> dict | None:
    """A shopper by id, for resolving a session token."""
    async with session_scope() as db:
        shopper = await db.get(Shopper, shopper_id)
        if shopper is None:
            return None
        return {
            "shopper_id": shopper.shopper_id,
            "connection_id": shopper.connection_id,
            "username": shopper.username,
            "display_name": shopper.display_name or shopper.username,
            "email": shopper.email,
        }


async def set_email(shopper_id: str, email: str) -> str:
    """The email an existing account will be confirmed at.

    For accounts created before the email field existed. The checkout gate prompts
    for one, and this is what the prompt writes back. Raises SignUpError for an
    address that is not one.
    """
    email = _validate_email(email)
    if email is None:
        raise SignUpError("An email is needed to send the order confirmation.")

    async with session_scope() as db:
        shopper = await db.get(Shopper, shopper_id)
        if shopper is None:
            raise SignUpError("That account no longer exists.")
        shopper.email = email
        return email