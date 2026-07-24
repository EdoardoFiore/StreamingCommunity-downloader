"""Authentication middleware and authorisation dependencies.

Authentication is global: a single ASGI middleware resolves the session cookie
for every request and rejects anything that is not on the public allowlist, so
forgetting a decorator cannot leave an endpoint open.

Authorisation is per-route, through ``require(...)``. Hiding a button in the UI
is not authorisation — every operation is checked here, server-side.
"""

import logging

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse, RedirectResponse

from app.auth import models
from app.auth import session as sessions
from app.auth.permissions import Permission
from app.config import AUTH_ENABLED, TRUST_PROXY_HEADERS

logger = logging.getLogger(__name__)

# Granted to every request when AUTH_ENABLED is False: the surface the panel
# exposed before Jellyfin SSO existed. REQUEST/MANAGE_REQUESTS/MANAGE_USERS
# are deliberately excluded — a queue and a user list have no meaning without
# accounts.
OPEN_MODE_PERMISSIONS = int(
    Permission.DOWNLOAD
    | Permission.MANAGE_SETTINGS
    | Permission.MANAGE_FILES
    | Permission.VIEW_LIBRARY
)

OPEN_MODE_USER = models.User(
    id=0,
    jellyfin_user_id="",
    username="Pannello",
    is_jellyfin_admin=False,
    permissions=OPEN_MODE_PERMISSIONS,
    enabled=True,
    device_id="",
    created_at="",
    last_login_at=None,
)

# Reachable without a session. Everything not listed here needs one.
PUBLIC_PATHS = {
    "/login",
    "/favicon.ico",
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/jellyfin",
}
PUBLIC_PREFIXES = ("/static/", "/docs/")

# API routes that need a session but no particular permission — they only ever
# expose the caller's own identity. Listed explicitly so that a new endpoint
# without a permission guard fails tests/test_permissions.py instead of quietly
# being reachable by everyone. Nothing may be added here casually.
SESSION_ONLY_PATHS = {
    "/api/auth/me",
    "/api/auth/logout",
}

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}

# Set on a CSRF-rejection response so the frontend can tell it apart from an
# ordinary 403 (permission denied) without parsing the message text. A stale
# token — a tab left open across a newer login elsewhere, which rotates the
# session cookie every tab shares but leaves each tab's own in-memory token
# behind — is safe to recover from automatically; a real permission denial is
# not something a token refresh would ever fix, and must not be retried.
CSRF_RETRY_HEADER = "X-CSRF-Retry"


def is_public(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def client_ip(request: Request) -> str | None:
    """Best-effort real client address, for Jellyfin's session log.

    Proxy headers are only trusted when TRUST_PROXY_HEADERS is set: otherwise
    any client could spoof the address the panel reports to Jellyfin.
    """
    if TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    return request.client.host if request.client else None


class AuthMiddleware:
    """Resolves the session, enforces authentication and checks CSRF."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope["state"] = scope.get("state", {})

        if not AUTH_ENABLED:
            # No Jellyfin server configured: every request is the same
            # implicit user. No cookie is ever set, so there is no session to
            # ride — the CSRF check below exists to protect that cookie and
            # does not apply here.
            scope["state"]["user"] = OPEN_MODE_USER
            scope["state"]["csrf_token"] = None
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        path = request.url.path
        scope["state"]["user"] = None
        scope["state"]["csrf_token"] = None

        raw_token = request.cookies.get(sessions.SESSION_COOKIE)
        resolved = sessions.resolve_session(raw_token) if raw_token else None
        if resolved is not None:
            user, csrf_token = resolved
            scope["state"]["user"] = user
            scope["state"]["csrf_token"] = csrf_token

        if is_public(path):
            await self.app(scope, receive, send)
            return

        if resolved is None:
            await self._unauthenticated(request, send, scope, receive)
            return

        if request.method not in SAFE_METHODS:
            supplied = request.headers.get(sessions.CSRF_HEADER, "")
            if not supplied or supplied != scope["state"]["csrf_token"]:
                logger.warning("CSRF token mismatch on %s %s", request.method, path)
                await JSONResponse(
                    {"detail": "Token CSRF mancante o non valido"},
                    status_code=403,
                    headers={CSRF_RETRY_HEADER: "1"},
                )(scope, receive, send)
                return

        await self.app(scope, receive, send)

    @staticmethod
    async def _unauthenticated(request: Request, send, scope, receive):
        wants_html = "text/html" in request.headers.get("accept", "")
        if not request.url.path.startswith("/api/") and wants_html:
            await RedirectResponse("/login", status_code=302)(scope, receive, send)
            return
        await JSONResponse({"detail": "Autenticazione richiesta"}, status_code=401)(
            scope, receive, send
        )


# ── Dependencies ───────────────────────────────────────────────────────────────

def current_user(request: Request):
    """The authenticated user. The middleware guarantees it is present."""
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401, detail="Autenticazione richiesta")
    return user


def require(*required: Permission, mode: str = "and"):
    """Dependency factory enforcing permission bits on a route or router."""

    def dependency(request: Request):
        user = current_user(request)
        if not user.has(*required, mode=mode):
            names = ", ".join(p.name for p in required)
            logger.warning(
                "User %s (id=%s) denied on %s %s: needs %s",
                user.username, user.id, request.method, request.url.path, names,
            )
            raise HTTPException(status_code=403, detail="Permesso negato")
        return user

    dependency.__required_permissions__ = tuple(required)
    return dependency
