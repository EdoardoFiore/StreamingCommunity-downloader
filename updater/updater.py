"""The optional service that updates the panel's container.

The panel cannot do this itself: it would need the Docker socket, which is root
on the host, in a process that parses third-party pages and that open mode opens
to anonymous visitors; and ``up -d`` would kill the process running it. So the
socket lives here, in a service with no network and one verb: pull the image the
compose file already names and recreate that one service. No parameter crosses
the wire, so a compromised panel can only ask for what an administrator's
``docker compose pull`` would do.

It listens on a Unix socket in a volume shared with the panel: no port to
publish and no token to paste, and owning the volume is the permission.

Standard library only, deliberately, since this process holds the socket.
"""

import json
import logging
import os
import socket
import socketserver
import stat
import subprocess
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler

SOCKET_PATH = os.getenv("UPDATER_SOCKET", "/run/scpanel-updater/updater.sock")
SERVICE = os.getenv("PANEL_SERVICE", "web")

INSPECT_TIMEOUT = 30
PULL_TIMEOUT = 900
UP_TIMEOUT = 300
# Enough of Docker's own words to act on, without shipping a whole pull log.
MAX_OUTPUT_CHARS = 2000

logger = logging.getLogger("updater")


class UpdateProblem(Exception):
    """Something the administrator has to fix; the message is shown to them."""


@dataclass
class Target:
    project: str
    working_dir: str
    config_files: list[str]
    env_files: list[str]
    image_ref: str
    image_id: str


def _run(args: list[str], timeout: int, cwd: str | None = None, env: dict | None = None) -> str:
    try:
        done = subprocess.run(
            args, cwd=cwd, env=env, timeout=timeout, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as e:
        raise UpdateProblem(f"Docker non ha risposto entro {timeout} secondi.") from e
    except OSError as e:
        raise UpdateProblem(f"Docker non si avvia: {e}") from e
    if done.returncode != 0:
        output = (done.stderr or done.stdout).strip()
        raise UpdateProblem(output[-MAX_OUTPUT_CHARS:] or f"Docker ha risposto {done.returncode}.")
    return done.stdout


def _inspect(reference: str, kind: str = "container") -> dict:
    return json.loads(_run(["docker", kind, "inspect", reference], INSPECT_TIMEOUT))[0]


def locate() -> Target:
    """The panel's container, found from this one's compose labels so nothing
    about the deployment has to be configured twice."""
    try:
        own = _inspect(socket.gethostname())
    except (UpdateProblem, ValueError, IndexError) as e:
        raise UpdateProblem(
            "L'updater non riesce a leggere il proprio container: "
            "verifica il mount di /var/run/docker.sock e di non aver impostato `hostname:`."
        ) from e
    project = (own["Config"].get("Labels") or {}).get("com.docker.compose.project")
    if not project:
        raise UpdateProblem("L'updater non è stato avviato con docker compose.")

    ids = _run(
        ["docker", "ps", "-aq",
         "--filter", f"label=com.docker.compose.project={project}",
         "--filter", f"label=com.docker.compose.service={SERVICE}"],
        INSPECT_TIMEOUT,
    ).split()
    if not ids:
        raise UpdateProblem(f"Nessun container del servizio '{SERVICE}' nel progetto '{project}'.")

    panel = _inspect(ids[0])
    labels = panel["Config"].get("Labels") or {}
    working_dir = labels.get("com.docker.compose.project.working_dir", "")
    config_files = [f for f in labels.get("com.docker.compose.project.config_files", "").split(",") if f]
    if not working_dir or not config_files:
        raise UpdateProblem("Il container del pannello non indica il suo file compose.")
    for path in config_files:
        # Same path as on the host, or every relative path in the file would
        # resolve to somewhere the daemon has never heard of.
        if not os.path.isfile(path):
            raise UpdateProblem(
                f"L'updater non vede {path}: la cartella del compose va montata allo "
                "stesso percorso che ha sull'host."
            )
    return Target(
        project=project,
        working_dir=working_dir,
        config_files=config_files,
        env_files=[f for f in labels.get("com.docker.compose.project.environment_file", "").split(",") if f],
        image_ref=panel["Config"]["Image"],
        image_id=panel["Image"],
    )


def _compose(target: Target, *args: str, timeout: int) -> str:
    command = ["docker", "compose", "-p", target.project, "--project-directory", target.working_dir]
    for path in target.config_files:
        command += ["-f", path]
    for path in target.env_files:
        command += ["--env-file", path]
    # PWD too: the documented volume line is ${PWD}:${PWD}, and this process's
    # own PWD interpolated into it would change the service's configuration.
    return _run(
        [*command, *args],
        timeout,
        cwd=target.working_dir,
        env={**os.environ, "PWD": target.working_dir},
    )


class Updater:
    """One update at a time. State lives here, not in the panel, because the
    panel is the thing being replaced while it runs."""

    def __init__(self):
        self._lock = threading.Lock()
        self.state = "idle"
        self.message = ""

    def _set(self, state: str, message: str = "") -> None:
        with self._lock:
            self.state, self.message = state, message
        logger.info("%s %s", state, message)

    def status(self) -> dict:
        with self._lock:
            state, message = self.state, self.message
        busy = state in ("pulling", "recreating")
        problem = None
        if not busy:
            try:
                locate()
            except UpdateProblem as e:
                problem = str(e)
        return {"state": state, "message": message, "ready": problem is None, "problem": problem}

    def start(self) -> bool:
        """False when one is already running. The state changes before this
        returns, so a poll straight after never reads the previous outcome."""
        with self._lock:
            if self.state in ("pulling", "recreating"):
                return False
            self.state, self.message = "pulling", ""
        threading.Thread(target=self.run, daemon=True).start()
        return True

    def run(self) -> None:
        try:
            target = locate()
            _compose(target, "pull", SERVICE, timeout=PULL_TIMEOUT)
            if _inspect(target.image_ref, "image")["Id"] == target.image_id:
                self._set("unchanged", target.image_ref)
                return
            self._set("recreating", target.image_ref)
            # --no-deps: this service must never be recreated by its own command.
            _compose(target, "up", "-d", "--no-deps", SERVICE, timeout=UP_TIMEOUT)
            self._set("done", target.image_ref)
        except UpdateProblem as e:
            self._set("failed", str(e))
        except Exception as e:
            logger.exception("Update failed")
            self._set("failed", f"Errore imprevisto: {e}")


class _Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


def handler_for(updater: Updater):
    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status: int, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/status":
                self._reply(200, updater.status())
            else:
                self._reply(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/update":
                self._reply(404, {"error": "not found"})
            elif updater.start():
                self._reply(202, {"state": "pulling"})
            else:
                self._reply(409, {"error": "Un aggiornamento è già in corso."})

        def address_string(self):
            return "panel"  # a Unix socket peer has no address

        def log_message(self, format, *args):
            logger.debug(format, *args)

    return Handler


def serve(path: str = SOCKET_PATH, updater: Updater | None = None) -> _Server:
    # A socket left by a previous run, in a volume that outlives this container.
    if os.path.exists(path) and stat.S_ISSOCK(os.stat(path).st_mode):
        os.unlink(path)
    server = _Server(path, handler_for(updater or Updater()))
    # The panel may run under any uid (#28). The volume is the permission: only
    # the services an administrator mounted it into can reach this file.
    os.chmod(path, 0o666)
    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    # Not fatal when the deployment is wrong: the panel reports the reason, and
    # that is the only place the administrator is going to look.
    try:
        locate()
        logger.info("Ready; watching service '%s'", SERVICE)
    except UpdateProblem as e:
        logger.warning("Not ready: %s", e)
    while True:
        try:
            with serve() as server:
                server.serve_forever()
        except OSError:
            logger.exception("Cannot listen on %s", SOCKET_PATH)
            time.sleep(30)


if __name__ == "__main__":
    main()
