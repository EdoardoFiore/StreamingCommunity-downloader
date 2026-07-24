"""Authentication routes: setup wizard, Jellyfin login, logout, current user."""

import asyncio
import logging
import math

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import db
from app.auth import models, ratelimit, session as sessions
from app.auth.deps import client_ip, current_user
from app.auth.jellyfin import (
    TOKEN_CHECK_DEVICE_ID,
    JellyfinAuthError,
    JellyfinClient,
    JellyfinError,
    JellyfinUnreachable,
    device_id_for,
    is_administrator,
    normalize_base_url,
)
from app.auth.permissions import ALL_PERMISSIONS
from app.config import AUTH_ENABLED, COOKIE_SAMESITE, COOKIE_SECURE

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


def _enforce_rate_limit(ip: str | None, username: str):
    """Reject before ever touching Jellyfin if this (ip, username) pair is
    still cooling down from previous failures."""
    wait = ratelimit.seconds_until_allowed(ip, username)
    if wait > 0:
        seconds = math.ceil(wait)
        raise HTTPException(
            status_code=429,
            detail=f"Troppi tentativi falliti. Riprova tra {seconds} secondi.",
            headers={"Retry-After": str(seconds)},
        )


class SetupRequest(BaseModel):
    url: str = Field(min_length=1)
    username: str = Field(min_length=1)
    password: str = ""


class LoginRequest(BaseModel):
    username: str = Field(min_length=1)
    password: str = ""


class TokenLoginRequest(BaseModel):
    token: str = Field(min_length=1)


def _set_session_cookie(response: Response, raw_token: str):
    response.set_cookie(
        sessions.SESSION_COOKIE,
        raw_token,
        max_age=int(sessions.SESSION_TTL.total_seconds()),
        httponly=True,
        samesite=COOKIE_SAMESITE,
        secure=COOKIE_SECURE,
        path="/",
    )


def _session_payload(user: models.User, csrf_token: str) -> dict:
    return {"user": user.to_public(), "csrf_token": csrf_token, "auth_enabled": AUTH_ENABLED}


def _authenticate(base_url: str, username: str, password: str, ip: str | None) -> dict:
    """Blocking Jellyfin authentication. Runs in a worker thread."""
    client = JellyfinClient(base_url, device_id=device_id_for(username), client_ip=ip)
    return client.authenticate(username, password)


def _whoami(base_url: str, token: str, ip: str | None) -> dict:
    """Blocking Jellyfin lookup for an already-issued token. Runs in a worker
    thread. Unlike ``_authenticate``, this never calls ``_drop_token``
    afterwards: the token belongs to the caller's own already-open Jellyfin
    session (e.g. the parent frame of the panel embedded as a Jellyfin custom
    tab), and invalidating it here would sign them out of Jellyfin too.
    """
    client = JellyfinClient(base_url, token=token, device_id=TOKEN_CHECK_DEVICE_ID, client_ip=ip)
    return client.me()


def _drop_token(base_url: str, username: str, token: str, ip: str | None):
    """Invalidate a user's Jellyfin access token.

    The panel never persists it: once identity is established the token has no
    further use here, and keeping it would be attack surface for no benefit.
    """
    JellyfinClient(
        base_url, token=token, device_id=device_id_for(username), client_ip=ip
    ).logout()


@router.get("/status")
def auth_status():
    """Public: tells the login page whether the panel still needs setting up."""
    if not AUTH_ENABLED:
        return {"setup_done": True, "jellyfin_url": None, "auth_enabled": False}
    url, _ = models.jellyfin_config()
    return {"setup_done": models.setup_done(), "jellyfin_url": url, "auth_enabled": True}


@router.post("/setup")
async def setup(body: SetupRequest, request: Request, response: Response):
    """First-run bootstrap: creates the panel administrator.

    Restricted to a user who is an administrator *on Jellyfin*, which is what
    removes the need for hardcoded credentials or a setup file.
    """
    if not AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Autenticazione disabilitata su questa installazione")
    if models.setup_done():
        raise HTTPException(status_code=403, detail="Il pannello è già configurato")

    try:
        base_url = normalize_base_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ip = client_ip(request)
    try:
        await asyncio.to_thread(JellyfinClient(base_url, client_ip=ip).public_info)
    except JellyfinError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    _enforce_rate_limit(ip, body.username)
    try:
        account = await asyncio.to_thread(
            _authenticate, base_url, body.username, body.password, ip
        )
    except JellyfinAuthError:
        ratelimit.record_failure(ip, body.username)
        raise HTTPException(status_code=401, detail="Credenziali Jellyfin non valide")
    except JellyfinUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    ratelimit.record_success(ip, body.username)

    jellyfin_user = account["User"]
    token = account["AccessToken"]
    try:
        if not is_administrator(jellyfin_user):
            logger.warning(
                "Setup attempted by non-administrator Jellyfin user %r",
                jellyfin_user.get("Name"),
            )
            raise HTTPException(
                status_code=403,
                detail="Solo un amministratore Jellyfin può configurare il pannello",
            )

        # The panel must be able to query Jellyfin on its own (to list users), so
        # it creates its own application key rather than asking anyone to paste
        # one. Done while the admin token is still valid.
        try:
            api_key = await asyncio.to_thread(
                JellyfinClient(base_url, token=token, client_ip=ip).create_api_key
            )
        except JellyfinError as exc:
            raise HTTPException(status_code=502, detail=f"Creazione API key fallita: {exc}")

        with db.tx() as conn:
            # Re-checked inside the write transaction: two concurrent setup
            # requests must not both create an administrator.
            if conn.execute("SELECT COUNT(*) AS n FROM jf_user").fetchone()["n"]:
                raise HTTPException(status_code=403, detail="Il pannello è già configurato")
            models.set_setting(models.SETTING_JELLYFIN_URL, base_url, conn)
            models.set_setting(models.SETTING_JELLYFIN_API_KEY, api_key, conn)
            models.set_setting(
                models.SETTING_JELLYFIN_SERVER_ID, jellyfin_user.get("ServerId", ""), conn
            )
            user = models.create_user(
                jellyfin_user_id=jellyfin_user["Id"],
                username=jellyfin_user.get("Name", body.username),
                permissions=int(ALL_PERMISSIONS),
                is_jellyfin_admin=True,
                conn=conn,
            )
    finally:
        await asyncio.to_thread(_drop_token, base_url, body.username, token, ip)

    models.touch_login(user.id, user.username, True)
    raw_token, csrf_token = sessions.create_session(user.id)
    _set_session_cookie(response, raw_token)
    logger.info("Panel bootstrapped by Jellyfin administrator %s", user.username)
    return _session_payload(models.get_user(user.id), csrf_token)


@router.post("/jellyfin")
async def login(body: LoginRequest, request: Request, response: Response):
    if not AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Autenticazione disabilitata su questa installazione")
    base_url, _ = models.jellyfin_config()
    if not base_url or not models.setup_done():
        raise HTTPException(status_code=409, detail="Pannello non ancora configurato")

    ip = client_ip(request)
    _enforce_rate_limit(ip, body.username)
    try:
        account = await asyncio.to_thread(
            _authenticate, base_url, body.username, body.password, ip
        )
    except JellyfinAuthError:
        logger.warning("Failed Jellyfin sign-in for %r", body.username)
        ratelimit.record_failure(ip, body.username)
        raise HTTPException(status_code=401, detail="Credenziali Jellyfin non valide")
    except JellyfinUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    ratelimit.record_success(ip, body.username)

    jellyfin_user = account["User"]
    await asyncio.to_thread(
        _drop_token, base_url, body.username, account["AccessToken"], ip
    )

    return _complete_login(jellyfin_user, response)


def _complete_login(jellyfin_user: dict, response: Response) -> dict:
    """Shared tail of both sign-in paths: match/auto-import the user, refuse
    a disabled account, open the panel session.

    Matched on the Jellyfin user id, never the username: usernames are
    renameable and would silently re-link an account after a rename.
    """
    user = models.get_user_by_jellyfin_id(jellyfin_user["Id"])

    if user is None:
        if not models.allow_new_jellyfin_login():
            logger.warning(
                "Sign-in refused for un-imported Jellyfin user %r", jellyfin_user.get("Name")
            )
            raise HTTPException(
                status_code=403,
                detail="Utente Jellyfin non abilitato sul pannello. "
                       "Chiedi a un amministratore di importarlo.",
            )
        user = models.create_user(
            jellyfin_user_id=jellyfin_user["Id"],
            username=jellyfin_user.get("Name", "?"),
            permissions=models.default_permissions(),
            is_jellyfin_admin=is_administrator(jellyfin_user),
        )
        logger.info("Auto-imported Jellyfin user %s on first sign-in", user.username)

    if not user.enabled:
        logger.warning("Sign-in refused for disabled panel user %s", user.username)
        raise HTTPException(status_code=403, detail="Account disabilitato sul pannello")

    models.touch_login(
        user.id, jellyfin_user.get("Name", user.username), is_administrator(jellyfin_user)
    )
    raw_token, csrf_token = sessions.create_session(user.id)
    _set_session_cookie(response, raw_token)
    return _session_payload(models.get_user(user.id), csrf_token)


@router.post("/jellyfin-token")
async def login_with_token(body: TokenLoginRequest, request: Request, response: Response):
    """Trade an already-issued Jellyfin access token for a panel session.

    For the panel embedded as a Jellyfin custom tab: the parent page already
    holds a valid token (``window.ApiClient``), so the visitor should not
    have to log in a second time. The token is never trusted on its own — it
    is checked live against the configured Jellyfin server on every call,
    exactly like a password would be. Not rate-limited like /jellyfin: an
    opaque Jellyfin token is not guessable, so this is not a credential
    brute-force surface the way a username/password pair is.
    """
    if not AUTH_ENABLED:
        raise HTTPException(status_code=404, detail="Autenticazione disabilitata su questa installazione")
    base_url, _ = models.jellyfin_config()
    if not base_url or not models.setup_done():
        raise HTTPException(status_code=409, detail="Pannello non ancora configurato")

    ip = client_ip(request)
    try:
        jellyfin_user = await asyncio.to_thread(_whoami, base_url, body.token, ip)
    except JellyfinAuthError:
        raise HTTPException(status_code=401, detail="Sessione Jellyfin non valida o scaduta")
    except JellyfinUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc))

    return _complete_login(jellyfin_user, response)


@router.post("/logout")
def logout(request: Request, response: Response):
    sessions.delete_session(request.cookies.get(sessions.SESSION_COOKIE, ""))
    response.delete_cookie(sessions.SESSION_COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(request: Request):
    user = current_user(request)
    return _session_payload(user, request.state.csrf_token)
