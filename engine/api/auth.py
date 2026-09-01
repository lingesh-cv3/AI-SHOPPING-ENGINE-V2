"""Who is calling, and what they may do.

One place, so the answer to "how does this route decide" is never spread across
several files.

The refusals are deliberately uninformative. A missing key, a fabricated key, a
revoked key and a key of the wrong kind all produce the same message, because
telling a caller which of those it was tells them what to try next.
"""

from __future__ import annotations

import logging

from fastapi import Header, HTTPException, Request

from engine import db

logger = logging.getLogger(__name__)

#: Said to every rejected caller, whatever the reason. Distinguishing a revoked key
#: from a fabricated one would confirm that the first was once real.
REFUSED = "that key is not valid for this request"


async def _resolved(header: str | None):
    """The key record behind an Authorization header, or None.

    Accepts both `Bearer <key>` and a bare key. Bearer is the correct form and what
    our own tools send; the bare form is accepted because somebody testing with curl
    will forget, and refusing them teaches nothing about security.
    """
    if not header:
        return None

    token = header[7:].strip() if header.lower().startswith("bearer ") else header.strip()
    if not token:
        return None

    try:
        return await db.keys.resolve(token)
    except Exception:  # noqa: BLE001
        # A database problem is not an authentication decision, but it cannot be
        # allowed to read as success either. Logged loudly, refused quietly.
        logger.exception("could not resolve an API key")
        return None


async def operator(authorization: str | None = Header(default=None)):
    """Require a CV3 operator key.

    The only kind that spans merchants, which is what makes one queue across every
    client possible - and the reason it is the one worth guarding hardest.
    """
    record = await _resolved(authorization)

    if record is None or record.kind != "operator":
        raise HTTPException(401, REFUSED)

    return record


def merchant_scoped(connection_id_field: str = "connection_id"):
    """Require a secret key, and one that speaks for the merchant being addressed.

    Two checks, and the second is the one that matters. Requiring *a* key stops
    strangers; requiring the *right* key stops one client reading another's, which
    is the failure that would actually end a contract.

    An operator key passes both, because CV3 staff work across clients and refusing
    them their own tooling would be theatre rather than security.

    Written as a factory because the connection appears under different names on
    different routes, and hard-coding one would quietly skip the check on the
    others - a check that silently does nothing is worse than no check, because it
    reads as protection.
    """

    async def dependency(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        record = await _resolved(authorization)

        if record is None or record.kind not in {"secret", "operator"}:
            raise HTTPException(401, REFUSED)

        if record.kind == "operator":
            return record

        target = request.path_params.get(connection_id_field)
        if target and record.connection_id != target:
            # Deliberately the same message as an invalid key. Saying "wrong
            # merchant" would confirm that the key is real and that the merchant
            # exists, which is two facts an attacker did not have.
            raise HTTPException(401, REFUSED)

        return record

    return dependency


#: Kinds that may act on a shopper's behalf.
#:
#: A publishable key belongs here and nowhere else. It ships in a browser, so it is
#: not a secret and never was - what makes it safe is that it is bound to one
#: merchant and reaches only these routes. Secret and operator keys are accepted too,
#: because a server-side caller doing shopper work is doing nothing a shopper could
#: not, and refusing them would only push people toward using a publishable key
#: server-side.
SHOPPER_KINDS = {"publishable", "secret", "operator"}


def shopper_scoped(connection_id_field: str = "connection_id"):
    """Require any key, and one entitled to speak for the merchant in the path.

    The second half is the point. Requiring a key stops strangers. Requiring the
    right key is what stops one merchant's storefront from driving another's -
    which, until now, was a single line of JSON away.
    """

    async def dependency(
        request: Request,
        authorization: str | None = Header(default=None),
    ):
        record = await _resolved(authorization)

        if record is None or record.kind not in SHOPPER_KINDS:
            raise HTTPException(401, REFUSED)

        if record.kind == "operator":
            return record

        target = request.path_params.get(connection_id_field)
        if target and record.connection_id != target:
            raise HTTPException(401, REFUSED)

        return record

    return dependency


async def any_key(authorization: str | None = Header(default=None)):
    """Require a key without checking which merchant it is for.

    For routes that take the connection in the body rather than the path, where the
    dependency cannot see it. The handler calls `belongs_to` afterwards - which is
    less tidy than doing it here, and the alternative is reading and re-parsing the
    body inside a dependency, which is worse.
    """
    record = await _resolved(authorization)

    if record is None or record.kind not in SHOPPER_KINDS:
        raise HTTPException(401, REFUSED)

    return record


def belongs_to(record, connection_id: str) -> None:
    """Raise unless this key may act for this merchant.

    Called by handlers that take the connection in the body. Kept as a function
    rather than repeated inline so there is one place to read when somebody asks how
    the check works.
    """
    if record.kind == "operator":
        return
    if record.connection_id != connection_id:
        raise HTTPException(401, REFUSED)
