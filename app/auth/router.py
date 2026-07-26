"""Authentication routes: setup wizard, Jellyfin login, logout, current user."""

import asyncio
import logging
import math

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import __version__, db
from app.auth import models, ratelimit, session as sessions
from app.auth.deps import client_ip, current_user, require
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
from app.auth.permissions import ALL_PERMISSIONS, Permission
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


class JellyfinConnectRequest(BaseModel):
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
    return {
        "user": user.to_public(),
        "csrf_token": csrf_token,
        "auth_enabled": AUTH_ENABLED and not models.runtime_open_mode(),
        # Shown in the UI so a bug report can name the build it came from. Here
        # rather than in the public /status: it is only useful to someone
        # already inside.
        "version": __version__,
    }


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


async def _authenticate_as_jellyfin_admin(
    base_url: str, username: str, password: str, ip: str | None
) -> tuple[dict, str]:
    """Authenticate against Jellyfin, require the account to be an
    administrator, and mint the panel's own service API key.

    Shared by /setup and /jellyfin-connect: both bootstrap the panel's
    Jellyfin identity the same way, just from different starting states.
    Rate-limited and counted like a normal login attempt — a correct password
    for a non-administrator account still clears the failure counter, since
    the credentials themselves were valid. The Jellyfin token is invalidated
    before returning either way; the panel never persists it. Raises
    HTTPException for bad credentials, an unreachable server, a
    non-administrator account, or a failed API key creation.
    """
    _enforce_rate_limit(ip, username)
    try:
        account = await asyncio.to_thread(_authenticate, base_url, username, password, ip)
    except JellyfinAuthError:
        ratelimit.record_failure(ip, username)
        raise HTTPException(status_code=401, detail="Credenziali Jellyfin non valide")
    except JellyfinUnreachable as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    ratelimit.record_success(ip, username)

    jellyfin_user = account["User"]
    token = account["AccessToken"]
    try:
        if not is_administrator(jellyfin_user):
            logger.warning(
                "Rejected: non-administrator Jellyfin user %r", jellyfin_user.get("Name")
            )
            raise HTTPException(
                status_code=403,
                detail="Solo un amministratore Jellyfin può configurare il pannello",
            )
        try:
            api_key = await asyncio.to_thread(
                JellyfinClient(base_url, token=token, client_ip=ip).create_api_key
            )
        except JellyfinError as exc:
            raise HTTPException(status_code=502, detail=f"Creazione API key fallita: {exc}")
    finally:
        await asyncio.to_thread(_drop_token, base_url, username, token, ip)

    return jellyfin_user, api_key


@router.get("/status")
def auth_status():
    """Public: tells the login page whether the panel still needs setting up."""
    if not AUTH_ENABLED or models.runtime_open_mode():
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

    jellyfin_user, api_key = await _authenticate_as_jellyfin_admin(
        base_url, body.username, body.password, ip
    )

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
        models.set_setting(models.SETTING_AUTH_MODE, "jellyfin", conn)
        user = models.create_user(
            jellyfin_user_id=jellyfin_user["Id"],
            username=jellyfin_user.get("Name", body.username),
            permissions=int(ALL_PERMISSIONS),
            is_jellyfin_admin=True,
            conn=conn,
        )

    models.touch_login(user.id, user.username, True)
    raw_token, csrf_token = sessions.create_session(user.id)
    _set_session_cookie(response, raw_token)
    logger.info("Panel bootstrapped by Jellyfin administrator %s", user.username)
    return _session_payload(models.get_user(user.id), csrf_token)


@router.post("/skip")
async def skip_setup():
    """First-run alternative to /setup: run the panel without Jellyfin at all.

    Interactive equivalent of deploying with AUTH_ENABLED=0, chosen from the
    setup wizard instead of an environment variable — no restart needed, and
    reachable again later from Settings via /jellyfin-connect. Reverting a
    "jellyfin" mode back to "open" is intentionally not supported.
    """
    if not AUTH_ENABLED:
        raise HTTPException(
            status_code=404, detail="Autenticazione disabilitata su questa installazione"
        )
    if models.setup_done():
        raise HTTPException(status_code=403, detail="Il pannello è già configurato")

    with db.tx() as conn:
        # Same race guard as /setup: two concurrent first-run choices must not
        # both win.
        if conn.execute("SELECT COUNT(*) AS n FROM jf_user").fetchone()["n"]:
            raise HTTPException(status_code=403, detail="Il pannello è già configurato")
        if models.get_setting(models.SETTING_AUTH_MODE) is not None:
            raise HTTPException(status_code=403, detail="Il pannello è già configurato")
        models.set_setting(models.SETTING_AUTH_MODE, "open", conn)

    logger.info("Panel setup skipped: running without Jellyfin login")
    return {"ok": True}


@router.post("/jellyfin-connect")
async def connect_jellyfin(
    body: JellyfinConnectRequest,
    request: Request,
    response: Response,
    user: models.User = Depends(
        require(Permission.MANAGE_SETTINGS, Permission.MANAGE_USERS, mode="or")
    ),
):
    """Connect the panel to Jellyfin from Settings.

    Covers two cases: the first connection after an admin skipped setup (every
    visitor holds MANAGE_SETTINGS in open mode — the same trust boundary as
    /setup being unauthenticated, a valid Jellyfin administrator account is
    what actually gates this), and reconfiguring an already-connected instance
    (new URL, rotated credentials), which requires MANAGE_USERS specifically
    since it can affect existing panel users.

    Pointing the panel at a genuinely different Jellyfin server orphans
    previously-imported users, whose jellyfin_user_id won't resolve there
    anymore — this is inherent to the feature, not silently reconciled, and
    must be called out in the Settings UI copy.
    """
    if not AUTH_ENABLED:
        raise HTTPException(
            status_code=404, detail="Autenticazione disabilitata su questa installazione"
        )

    already_connected = models.get_setting(models.SETTING_AUTH_MODE) == "jellyfin"
    if already_connected and not user.has(Permission.MANAGE_USERS):
        raise HTTPException(status_code=403, detail="Permesso negato")

    try:
        base_url = normalize_base_url(body.url)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    ip = client_ip(request)
    try:
        await asyncio.to_thread(JellyfinClient(base_url, client_ip=ip).public_info)
    except JellyfinError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    jellyfin_user, api_key = await _authenticate_as_jellyfin_admin(
        base_url, body.username, body.password, ip
    )

    with db.tx() as conn:
        if not already_connected and models.get_setting(models.SETTING_AUTH_MODE) == "jellyfin":
            # Same race as /setup and /skip: in open mode every visitor holds
            # MANAGE_SETTINGS, so two concurrent first-time connections are
            # effectively two concurrent public bootstraps.
            raise HTTPException(
                status_code=409, detail="Il pannello è già stato collegato a Jellyfin"
            )
        models.set_setting(models.SETTING_JELLYFIN_URL, base_url, conn)
        models.set_setting(models.SETTING_JELLYFIN_API_KEY, api_key, conn)
        models.set_setting(
            models.SETTING_JELLYFIN_SERVER_ID, jellyfin_user.get("ServerId", ""), conn
        )
        models.set_setting(models.SETTING_AUTH_MODE, "jellyfin", conn)
        panel_user = models.get_user_by_jellyfin_id(jellyfin_user["Id"])
        if panel_user is None:
            panel_user = models.create_user(
                jellyfin_user_id=jellyfin_user["Id"],
                username=jellyfin_user.get("Name", body.username),
                permissions=int(ALL_PERMISSIONS),
                is_jellyfin_admin=True,
                conn=conn,
            )

    models.touch_login(panel_user.id, jellyfin_user.get("Name", panel_user.username), True)
    raw_token, csrf_token = sessions.create_session(panel_user.id)
    _set_session_cookie(response, raw_token)
    logger.info("Panel connected to Jellyfin by administrator %s", panel_user.username)
    return _session_payload(models.get_user(panel_user.id), csrf_token)


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
