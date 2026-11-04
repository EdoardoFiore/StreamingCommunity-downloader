"""The panel's side of the optional updater service (see updater/updater.py).

Never holds the Docker socket and never says what to run: it can ask for the
state and ask for the one update there is. The service is absent on most
installs, which is an ordinary answer and not an error.
"""

import http.client
import json
import logging
import os
import socket

logger = logging.getLogger(__name__)

SOCKET_PATH = os.getenv("UPDATER_SOCKET", "/run/scpanel-updater/updater.sock")
# The service's status runs a few `docker inspect`, and this is awaited by a page.
TIMEOUT = 10


class UpdaterUnavailable(Exception):
    pass


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("updater", timeout=TIMEOUT)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(TIMEOUT)
        self.sock.connect(self._path)


def _call(method: str, path: str) -> tuple[int, dict]:
    connection = _UnixConnection(SOCKET_PATH)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    except (OSError, http.client.HTTPException, ValueError) as e:
        raise UpdaterUnavailable(str(e)) from e
    finally:
        connection.close()


def status() -> dict | None:
    """What the service reports, or None when there is no service to ask."""
    # Checked first so an install without the service costs no connection attempt.
    if not os.path.exists(SOCKET_PATH):
        return None
    try:
        _, payload = _call("GET", "/status")
    except UpdaterUnavailable as e:
        # A file left in the volume by a service that has since been stopped.
        logger.warning("Updater not reachable: %s", e)
        return None
    return payload


def start() -> tuple[int, dict]:
    """Ask for the update. Raises UpdaterUnavailable when the service cannot be reached."""
    return _call("POST", "/update")
