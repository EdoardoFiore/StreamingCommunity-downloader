"""The MCP endpoint, served by the panel itself at ``/mcp``.

Streamable HTTP, not SSE: SSE is the transport the MCP specification
deprecated, and it needed a long-lived stream per client. Being an ordinary
route of the panel is the other half of the point. An earlier version ran a
second uvicorn on its own port in a daemon thread, which meant a second port to
publish in Docker, a second bind address (it defaulted to 0.0.0.0 even on a
panel bound to localhost), and no reverse proxy or TLS in front of it unless
someone configured one twice. Here it inherits all of that from the panel.

**Stateless, JSON responses.** Each POST carries one JSON-RPC exchange and
gets one JSON answer: no session to keep alive, nothing held in memory between
calls — which also keeps it honest under the panel's single-process rule. None
of the tools streams anything, so the event stream would buy nothing.

Host and Origin validation is off: the panel is reached under whatever name its
reverse proxy gives it, and the bearer token is what a DNS-rebinding page
cannot produce. A browser cannot send that header cross-origin either, since
the panel answers no CORS preflight.
"""

import asyncio
import contextlib
import logging

from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from app.config import get_settings
from app.mcp import auth
from app.mcp.tools import mcp_server

logger = logging.getLogger(__name__)

PATH = "/mcp"

_session_manager = None


@contextlib.asynccontextmanager
async def running():
    """Run the session manager for the lifetime of the app.

    A manager runs once per instance, and a lifespan can be entered more than
    once in a process (each test client does), so a new one is built each time.
    """
    global _session_manager
    mcp_server.streamable_http_app(
        streamable_http_path=PATH,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    manager = mcp_server.session_manager
    async with manager.run():
        _session_manager = manager
        try:
            yield
        finally:
            _session_manager = None


def _bearer(scope) -> str:
    for name, value in scope.get("headers", []):
        if name == b"authorization":
            scheme, _, credential = value.decode("latin-1").partition(" ")
            if scheme.lower() == "bearer":
                return credential.strip()
    return ""


class _Endpoint:
    """ASGI app for ``/mcp``: switch, bearer token, then the MCP transport.

    A class rather than a function because Starlette wraps a plain function
    route as a request/response handler, and the transport needs raw ASGI.

    The token travels only in the Authorization header. A ``?token=`` fallback
    existed for clients that cannot set headers, but a query string ends up in
    every proxy's access log.
    """

    async def __call__(self, scope, receive, send):
        if not get_settings().get("mcp_enabled"):
            await JSONResponse({"detail": "Server MCP disattivato"}, status_code=404)(scope, receive, send)
            return

        user = await asyncio.to_thread(auth.authenticate, _bearer(scope))
        if user is None:
            logger.warning("Rejected MCP request without a valid token")
            await JSONResponse(
                {"detail": "Token MCP mancante o non valido"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )(scope, receive, send)
            return

        if _session_manager is None:
            await JSONResponse({"detail": "Server MCP non avviato"}, status_code=503)(scope, receive, send)
            return
        await _session_manager.handle_request(scope, receive, send)


endpoint = _Endpoint()
