"""Minimal Jellyfin HTTP client.

Synchronous on purpose: it uses ``requests`` like the rest of ``app/core`` and
callers wrap it in ``asyncio.to_thread``, matching app/routers/search.py.
"""

import base64
import logging
from urllib.parse import quote, urlparse

import requests

logger = logging.getLogger(__name__)

CLIENT_NAME = "SC Panel"
CLIENT_VERSION = "1.0.0"
API_KEY_APP_NAME = "SC Panel"
TIMEOUT = 15

# Device identifier used for the panel's own calls (listing users, reading system
# info with the service API key). Fixed, so those never show up as new devices.
SERVICE_DEVICE_ID = base64.urlsafe_b64encode(b"SCPanel_service").decode().rstrip("=")


class JellyfinError(Exception):
    """Base error. ``status`` is the HTTP status when there was a response."""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class JellyfinUnreachable(JellyfinError):
    """The server could not be contacted, or did not answer like Jellyfin."""


class JellyfinAuthError(JellyfinError):
    """Credentials or token rejected."""


def device_id_for(username: str) -> str:
    """Stable per-user device identifier.

    Jellyfin creates a session per DeviceId it sees. A fresh one on every login
    fills the Jellyfin dashboard with phantom devices and pushes users into
    per-account device limits, so this is derived deterministically and reused.

    It is keyed on the username rather than the (immutable) Jellyfin user id
    because the DeviceId has to travel on the authentication call itself, before
    any id is known. A rename therefore costs one extra device, once — against
    one per login for a random identifier. The panel's own identity link stays
    on the user id; this value is only what Jellyfin sees.

    One device per user rather than one for the whole panel: Jellyfin keys
    sessions by DeviceId, so a shared value would have users evicting each other.
    """
    raw = f"SCPanel_{(username or '').strip().lower()}".encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def normalize_base_url(url: str) -> str:
    """Validate and normalise a user-supplied Jellyfin base URL."""
    url = (url or "").strip().rstrip("/")
    if not url:
        raise ValueError("URL Jellyfin mancante")
    if "://" not in url:
        url = "http://" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("L'URL Jellyfin deve iniziare con http:// o https://")
    if not parsed.hostname:
        raise ValueError("URL Jellyfin non valido")
    return url


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        device_id: str = SERVICE_DEVICE_ID,
        client_ip: str | None = None,
    ):
        self.base_url = normalize_base_url(base_url)
        self.token = token
        self.device_id = device_id
        self.client_ip = client_ip

    # ── Request plumbing ──────────────────────────────────────────────────────

    def _auth_header(self) -> str:
        value = (
            f'MediaBrowser Client="{CLIENT_NAME}", Device="{CLIENT_NAME}", '
            f'DeviceId="{self.device_id}", Version="{CLIENT_VERSION}"'
        )
        if self.token:
            value += f', Token="{self.token}"'
        return value

    def _headers(self, forward_ip: bool) -> dict:
        headers = {
            "Authorization": self._auth_header(),
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if forward_ip and self.client_ip:
            headers["X-Forwarded-For"] = self.client_ip
        return headers

    def _request(self, method: str, path: str, *, json=None, authenticated=True):
        """Issue a request, retrying once without X-Forwarded-For on failure.

        Jellyfin sessions record an IP. Without X-Forwarded-For the server logs
        the panel's own address for every user, so we send the real one. The
        retry exists because Jellyfin rejects the forwarded address when the
        panel is not listed in its KnownProxies, and because the LDAP plugin
        applies network restrictions to it — in those setups the header turns
        every login into a failure. We log a warning on the retry so an admin can
        see why user IPs stopped propagating instead of silently losing them.
        """
        url = f"{self.base_url}{path}"
        forward = bool(self.client_ip)
        for attempt in (0, 1):
            try:
                response = requests.request(
                    method,
                    url,
                    json=json,
                    headers=self._headers(forward_ip=forward and attempt == 0),
                    timeout=TIMEOUT,
                )
            except requests.RequestException as exc:
                raise JellyfinUnreachable(f"Server Jellyfin non raggiungibile: {exc}") from exc

            if response.ok:
                return response
            if forward and attempt == 0:
                logger.warning(
                    "Jellyfin %s %s failed with HTTP %d while forwarding the client IP; "
                    "retrying without X-Forwarded-For. Jellyfin will log the panel's own "
                    "address for this user. Add the panel to Jellyfin's known proxies to fix it.",
                    method, path, response.status_code,
                )
                continue

            if response.status_code in (401, 403):
                raise JellyfinAuthError(
                    "Credenziali Jellyfin non valide", status=response.status_code
                )
            raise JellyfinError(
                f"Jellyfin ha risposto HTTP {response.status_code}", status=response.status_code
            )
        raise JellyfinUnreachable("Server Jellyfin non raggiungibile")

    @staticmethod
    def _json(response):
        try:
            return response.json()
        except ValueError as exc:
            raise JellyfinUnreachable(
                "Risposta non valida dal server Jellyfin (l'URL punta davvero a Jellyfin?)"
            ) from exc

    # ── Endpoints ─────────────────────────────────────────────────────────────

    def public_info(self) -> dict:
        """GET /System/Info/Public — unauthenticated, used to validate the URL."""
        return self._json(self._request("GET", "/System/Info/Public"))

    def system_info(self) -> dict:
        """GET /System/Info — authenticated; succeeding means the token is valid."""
        return self._json(self._request("GET", "/System/Info"))

    def authenticate(self, username: str, password: str) -> dict:
        """POST /Users/AuthenticateByName. Returns the raw ``{User, AccessToken}``."""
        data = self._json(
            self._request(
                "POST",
                "/Users/AuthenticateByName",
                json={"Username": username, "Pw": password},
            )
        )
        if not data.get("AccessToken") or not data.get("User", {}).get("Id"):
            raise JellyfinAuthError("Risposta di autenticazione Jellyfin incompleta")
        return data

    def logout(self):
        """POST /Sessions/Logout — invalidates ``self.token``.

        Called right after a login so the panel never keeps a user's Jellyfin
        access token around: it is not needed after identity is established.
        """
        try:
            self._request("POST", "/Sessions/Logout")
        except JellyfinError as exc:
            logger.warning("Could not invalidate the Jellyfin access token: %s", exc)

    def users(self) -> list[dict]:
        """GET /Users — needs the service API key (administrator scope)."""
        return self._json(self._request("GET", "/Users"))

    def create_api_key(self, app_name: str = API_KEY_APP_NAME) -> str:
        """Create an application API key and read it back.

        Jellyfin's POST returns no body, so the key list is fetched and the
        newest entry for this app name is taken.
        """
        self._request("POST", f"/Auth/Keys?App={quote(app_name)}")
        payload = self._json(self._request("GET", "/Auth/Keys"))
        items = payload.get("Items", payload if isinstance(payload, list) else [])
        for item in reversed(items):
            if item.get("AppName") == app_name and item.get("AccessToken"):
                return item["AccessToken"]
        raise JellyfinError("API key creata ma non ritrovata nell'elenco di Jellyfin")


def is_administrator(jellyfin_user: dict) -> bool:
    return bool(jellyfin_user.get("Policy", {}).get("IsAdministrator"))
