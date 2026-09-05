"""Who is calling, and what they may do.

One place, so the answer to "how does this route decide" is never spread across
several files.

The refusals are deliberately uninformative. A missing key, a fabricated key, a
revoked key and a key of the wrong kind all produce the same message, because
telling a caller which of those it was tells them what to try next.
"""

from __future__ import annotations

import hashlib
import logging
import os

from fastapi import Cookie, Header, HTTPException, Request, Response

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


# ---------------------------------------------------------------------------
# Which shopper, as opposed to which merchant
# ---------------------------------------------------------------------------
#
# Everything above answers "may this caller act for this merchant". That was the
# only question being asked, and it is not enough. A publishable key is bound to
# one shop and ships in the browser, so every shopper at that shop holds the same
# one - and cart ids run BSK00001, BSK00002. Reading somebody else's basket was
# counting, not attacking.
#
# So there is a second identity, underneath the key: the browser itself. It is not
# an account, because most shoppers never make one and a guest's basket needs
# protecting just as much. It is a cookie the page cannot read and nobody chose.


#: The cookie that says which browser this is.
#:
#: httpOnly for the same reason as the sign-in cookie: an injected script can steal
#: anything JavaScript can read. This one is not a credential in the usual sense -
#: it identifies rather than authenticates - but it is what stands between a
#: stranger and a shopper's basket, which is close enough to treat it the same way.
VISITOR_COOKIE = "cv3_visitor"

#: A year. A basket should still be theirs when they come back after a fortnight,
#: and a guest has nothing else remembering them.
VISITOR_MAX_AGE = 365 * 24 * 60 * 60


def _visitor_key(token: str) -> str:
    """The stored form of a visitor token.

    Hashed, so the database never holds anything that could be pasted back into a
    cookie. Same reasoning as the API keys and the shopper sessions: the token is
    32 bytes of urandom, so a fast hash is right and there is no dictionary to
    slow anybody down.
    """
    return "vis_" + hashlib.sha256(token.encode()).hexdigest()[:40]


class Visitor:
    """One browser, and whoever is signed in on it.

    Carries both because a caller is legitimately two people at once. A shopper who
    filled a basket as a guest and then signed in must not lose it, and the same
    shopper on their phone tomorrow must still find it - so the browser's claim and
    the account's claim both have to count.
    """

    def __init__(self, key: str, shopper_cookie: str | None) -> None:
        self.key = key
        self._shopper_cookie = shopper_cookie

    async def keys_for(self, connection_id: str) -> list[str]:
        """Every identity this caller may act as, most specific first.

        The account comes first deliberately. Whatever they make while signed in is
        filed under the account rather than under this browser, which is what lets
        it follow them to another device. A guest's things are filed under the
        browser, because there is nothing else to file them under.
        """
        session = await db.shopper_sessions.resolve(
            self._shopper_cookie, connection_id
        )
        if session is not None:
            return [session["shopper_id"], self.key]
        return [self.key]


async def visitor(
    request: Request,
    response: Response,
    cv3_visitor: str | None = Cookie(default=None),
    cv3_shopper: str | None = Cookie(default=None),
) -> Visitor:
    """Identify the browser, minting a cookie for it if it has none.

    Minted here rather than at some explicit "start a visit" endpoint, because
    there is no such moment - a shopper arrives by loading a page, and the first
    thing the storefront does is ask for a cart. Any route that can own something
    is a route that can be somebody's first.
    """
    token = cv3_visitor

    if not token or len(token) < 16:
        token = f"vis_{os.urandom(32).hex()}"
        response.set_cookie(
            VISITOR_COOKIE,
            token,
            max_age=VISITOR_MAX_AGE,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )

    return Visitor(_visitor_key(token), cv3_shopper)


async def require_owner(
    who: Visitor,
    connection_id: str,
    kind: str,
    resource_id: str,
    *,
    code: str,
    message: str,
) -> None:
    """Refuse unless this caller owns this cart, conversation or order.

    404 rather than 403, and that is the whole point of the status code here. A
    403 confirms the thing exists, so an id that answers "forbidden" is an id
    somebody has just learned is real. Refusing the way a missing thing refuses
    tells them nothing they did not already have.
    """
    if await db.owners.may_touch(
        connection_id, kind, resource_id, await who.keys_for(connection_id)
    ):
        return

    raise HTTPException(
        404,
        detail={"code": code, "message": message, "retryable": False},
    )
