"""Sign up, sign in, sign out, and who am I.

The cookie is httpOnly, so no script on the page can read it. That is the whole
reason for a cookie rather than a token in localStorage: an injected script can steal
anything JavaScript can read, and it cannot read this.

SameSite=Lax rather than None, which requires the storefront and engine to share an
origin. Vite proxies /api to the engine so that holds in development too - and
SameSite=None would need HTTPS and give up the browser's own CSRF protection, which
is a worse place to arrive at by accident.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from engine import db
from engine.db.shoppers import SignUpError
from engine.session import shopper_memory

from .auth import any_key, belongs_to

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/account", tags=["account"])

#: The cookie the browser holds. Named for what it is.
COOKIE = "cv3_shopper"

#: Said to every rejected sign-in, whichever half was wrong.
#:
#: One message for a wrong username and a wrong password, because telling somebody
#: which they got right tells them who shops here.
REFUSED = "That username and password do not match."


class SignUp(BaseModel):
    connection_id: str
    username: str = Field(min_length=3, max_length=80)
    password: str = Field(min_length=8, max_length=200)
    display_name: str | None = Field(default=None, max_length=80)
    #: The basket in this tab, if they had put anything in one before signing in.
    guest_cart: str | None = None
    #: The conversation they were having before signing in, if any.
    #:
    #: Adopted onto the account, because signing in is meant to give a shopper
    #: their context - losing the conversation they are in the middle of, at the
    #: exact moment they identify themselves, would be backwards.
    guest_session: str | None = None


class SignIn(BaseModel):
    connection_id: str
    username: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=1, max_length=200)
    guest_session: str | None = None
        #: The basket in this tab, if they had put anything in one before signing in.
    guest_cart: str | None = None


def _set_cookie(response: Response, request: Request, token: str) -> None:
    """Put the session in an httpOnly cookie.

    Secure only when the request arrived over HTTPS, so development on localhost
    works without a certificate and production gets the flag automatically. Hard
    coding it either way means either a broken local setup or a cookie sent in
    clear over the wire.
    """
    response.set_cookie(
        COOKIE,
        token,
        max_age=db.shopper_sessions.SESSION_DAYS * 24 * 60 * 60,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https",
        path="/",
    )


async def _finish(
    shopper: dict,
    connection_id: str,
    guest_session: str | None,
    guest_cart: str | None,
) -> dict:
    """Give the shopper their conversation and their basket back.

    The session id is derived from the shopper and the merchant rather than stored,
    so there is one source of truth. Three separate bugs this week came from two
    places holding one value.

    The cart is the interesting half. A returning shopper has a basket here, and
    they may also have put something in a basket in this tab before signing in.
    Neither can simply win:

      dropping the tab's cart loses something somebody just chose, which is the
      worse outcome and the one they would notice

      dropping the remembered cart quietly discards a basket they built last week,
      which they would notice later and trust us less for

    So the tab's cart is kept and remembered as theirs when there is one, and the
    older basket is left on the platform rather than deleted - we stop pointing at
    it, and nothing is destroyed on somebody's behalf.

    Merging the two properly means reading both and moving lines, which is several
    platform calls and a reconciliation that can half-fail. Worth doing when a real
    shopper complains; not worth guessing at now.
    """
    account_session = await shopper_memory.session_for(
        shopper["shopper_id"], connection_id
    )

    if guest_session:
        moved = await shopper_memory.adopt(
            guest_session, account_session, connection_id
        )
        if moved:
            logger.info(
                "adopted %s guest turn(s) onto %s", moved, shopper["shopper_id"]
            )

    remembered = await db.shopper_carts.remembered(
        shopper["shopper_id"], connection_id
    )

    if guest_cart:
        # What they are holding now wins, and becomes theirs.
        await db.shopper_carts.remember(
            shopper["shopper_id"], connection_id, guest_cart
        )
        cart_id = guest_cart
    else:
        cart_id = remembered

    return {"session_id": account_session, "cart_id": cart_id}


@router.post("/signup")
async def sign_up(
    body: SignUp,
    request: Request,
    response: Response,
    key=Depends(any_key),
) -> dict:
    """Create an account and sign in.

    Signed in immediately, because making somebody sign up and then sign in with
    the details they just typed is a step that exists only because it was easier
    to build that way.
    """
    belongs_to(key, body.connection_id)

    try:
        shopper = await db.shoppers.create(
            body.connection_id,
            body.username,
            body.password,
            display_name=body.display_name,
        )
    except SignUpError as exc:
        # 400 with the reason, because these are all things the person can fix -
        # a taken username, a short password - and hiding why would be unkind
        # rather than secure.
        raise HTTPException(400, str(exc)) from exc

    token = await db.shopper_sessions.start(
        shopper["shopper_id"], body.connection_id
    )
    _set_cookie(response, request, token)

    restored = await _finish(
        shopper, body.connection_id, body.guest_session, body.guest_cart
    )

    return {
        "username": shopper["username"],
        "display_name": shopper["display_name"],
        **restored,
    }


@router.post("/signin")
async def sign_in(
    body: SignIn,
    request: Request,
    response: Response,
    key=Depends(any_key),
) -> dict:
    """Sign in, unless this username has been guessed at too often lately."""
    belongs_to(key, body.connection_id)

    if await db.shopper_sessions.locked_out(body.connection_id, body.username):
        # 429 rather than 401, and the reason said plainly: somebody locked out by
        # their own mistyping needs to know that waiting fixes it, and somebody
        # guessing learns only that guessing has stopped working.
        raise HTTPException(
            429, "Too many failed attempts. Wait fifteen minutes and try again."
        )

    shopper = await db.shoppers.verify(
        body.connection_id, body.username, body.password
    )

    if shopper is None:
        await db.shopper_sessions.record_failure(body.connection_id, body.username)
        raise HTTPException(401, REFUSED)

    await db.shopper_sessions.clear_failures(body.connection_id, body.username)

    # A brand new token, never one the request supplied. A token already sitting in
    # the cookie could have been planted there, and extending it would sign the
    # planter in as this shopper.
    token = await db.shopper_sessions.start(
        shopper["shopper_id"], body.connection_id
    )
    _set_cookie(response, request, token)
    restored = await _finish(
        shopper, body.connection_id, body.guest_session, body.guest_cart
    )

    return {
        "username": shopper["username"],
        "display_name": shopper["display_name"],
        **restored,
    }


@router.post("/signout")
async def sign_out(
    response: Response,
    cv3_shopper: str | None = Cookie(default=None),
) -> dict:
    """End this session.

    Needs no key and no valid session. Signing out is not a privileged operation,
    and refusing to sign somebody out because their session had already expired
    would be absurd.
    """
    await db.shopper_sessions.end(cv3_shopper)
    response.delete_cookie(COOKIE, path="/")
    return {"signed_out": True}


@router.get("/me")
async def me(
    connection_id: str,
    key=Depends(any_key),
    cv3_shopper: str | None = Cookie(default=None),
) -> dict:
    """Who is signed in here, if anybody.

    A guest is not an error. Most shoppers will never sign in, so this returns
    signed_in false rather than a 401 - the storefront asks on every load, and an
    error for the normal case would be the wrong shape.
    """
    belongs_to(key, connection_id)

    session = await db.shopper_sessions.resolve(cv3_shopper, connection_id)
    if session is None:
        return {"signed_in": False}

    shopper = await db.shoppers.by_id(session["shopper_id"])
    if shopper is None:
        # The account is gone but the session was not. Treated as signed out.
        return {"signed_in": False}

    return {
        "signed_in": True,
        "username": shopper["username"],
        "display_name": shopper["display_name"],
        #: Which conversation to use. Derived from the shopper and the merchant,
        #: so the storefront never has to keep a copy in step.
        "session_id": await shopper_memory.session_for(
            session["shopper_id"], connection_id
        ),
        #: Their basket, so a returning shopper does not start empty.
        "cart_id": await db.shopper_carts.remembered(
            session["shopper_id"], connection_id
        ),
    }



class MyCart(BaseModel):
    connection_id: str
    cart_id: str


@router.post("/cart")
async def claim_cart(
    body: MyCart,
    key=Depends(any_key),
    cv3_shopper: str | None = Cookie(default=None),
) -> dict:
    """Note that this cart belongs to whoever is signed in.

    Called whenever a signed-in shopper's cart changes, not only when they sign in
    - which is the bug this fixes. The cart was remembered once, at sign-in, so
    anything added afterwards was never theirs: clearing browser storage lost the
    basket while the conversation survived, because only one of them was being kept.

    A guest is not an error. Most shoppers never sign in, and this is called on
    every cart change - so returning quietly is the common case rather than a
    failure.
    """
    belongs_to(key, body.connection_id)

    session = await db.shopper_sessions.resolve(cv3_shopper, body.connection_id)
    if session is None:
        return {"remembered": False}

    await db.shopper_carts.remember(
        session["shopper_id"], body.connection_id, body.cart_id
    )
    return {"remembered": True}



