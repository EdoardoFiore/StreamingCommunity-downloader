"""The updater service and the panel's way of asking it.

The service holds the Docker socket, so these pin what it will run and when it
refuses. Docker itself is faked at the subprocess boundary; the socket between
panel and service is real.
"""

import json
import os
import subprocess
import threading
import time

import pytest

from app import updater_client, updates
from app.jobs import job_manager
from tests.conftest import enable_open_mode
from updater import updater as service


class FakeDocker:
    """Answers the commands the service runs, and records them."""

    def __init__(self, compose_file):
        self.compose_file = str(compose_file)
        self.commands = []
        self.env = None
        self.new_image = "sha256:new"
        self.fail_on = None

    def __call__(self, args, cwd=None, env=None, timeout=None, **kwargs):
        self.commands.append(args)
        out = ""
        joined = " ".join(args)
        if self.fail_on and self.fail_on in joined:
            return subprocess.CompletedProcess(args, 1, "", "denied: boom")
        if args[:3] == ["docker", "container", "inspect"] and args[3] == "panel-id":
            out = json.dumps([{"Image": "sha256:old", "Config": {
                "Image": "ghcr.io/x/panel:latest",
                "Labels": {
                    "com.docker.compose.project.working_dir": os.path.dirname(self.compose_file),
                    "com.docker.compose.project.config_files": self.compose_file,
                },
            }}])
        elif args[:3] == ["docker", "container", "inspect"]:
            out = json.dumps([{"Config": {"Labels": {"com.docker.compose.project": "stack"}}}])
        elif args[:2] == ["docker", "ps"]:
            out = "panel-id\n"
        elif args[:3] == ["docker", "image", "inspect"]:
            out = json.dumps([{"Id": self.new_image}])
        if args[:2] == ["docker", "compose"]:
            self.env = env
        return subprocess.CompletedProcess(args, 0, out, "")

    @property
    def compose_verbs(self):
        return [c[c.index("-f") + 2:] for c in self.commands if c[:2] == ["docker", "compose"]]


@pytest.fixture
def docker(tmp_path, monkeypatch):
    compose_file = tmp_path / "docker-compose.yml"
    compose_file.write_text("services: {}")
    fake = FakeDocker(compose_file)
    monkeypatch.setattr(service.subprocess, "run", fake)
    return fake


def test_an_update_pulls_then_recreates_only_the_panel(docker):
    updater = service.Updater()

    updater.run()

    assert updater.state == "done"
    assert docker.compose_verbs == [["pull", "web"], ["up", "-d", "--no-deps", "web"]]


def test_compose_runs_as_it_would_on_the_host(docker):
    service.Updater().run()

    command = next(c for c in docker.commands if c[:2] == ["docker", "compose"])
    folder = os.path.dirname(docker.compose_file)
    assert command[2:8] == ["-p", "stack", "--project-directory", folder, "-f", docker.compose_file]
    # The documented volume line interpolates it.
    assert docker.env["PWD"] == folder


def test_the_same_image_recreates_nothing(docker):
    docker.new_image = "sha256:old"
    updater = service.Updater()

    updater.run()

    assert updater.state == "unchanged"
    assert docker.compose_verbs == [["pull", "web"]]


def test_a_failed_pull_leaves_the_running_panel_alone(docker):
    docker.fail_on = "pull"
    updater = service.Updater()

    updater.run()

    assert updater.state == "failed"
    assert "denied: boom" in updater.message
    assert docker.compose_verbs == [["pull", "web"]]


def test_a_compose_file_it_cannot_see_is_named(docker):
    os.unlink(docker.compose_file)

    status = service.Updater().status()

    assert status["ready"] is False
    assert docker.compose_file in status["problem"]


def test_a_second_update_is_refused_while_one_runs(docker, monkeypatch):
    release = threading.Event()
    monkeypatch.setattr(service.Updater, "run", lambda self: release.wait(5))
    updater = service.Updater()

    assert updater.start() is True
    assert updater.start() is False
    release.set()


def test_start_changes_the_state_before_returning(docker, monkeypatch):
    """Otherwise the panel's first poll reads the previous attempt's outcome."""
    monkeypatch.setattr(service.Updater, "run", lambda self: None)
    updater = service.Updater()
    updater.state = "failed"

    updater.start()

    assert updater.state == "pulling"


# ── Over the socket ──────────────────────────────────────────────────────────


@pytest.fixture
def socket_path(tmp_path_factory, monkeypatch):
    # Not under tmp_path: a Unix socket path has a short length limit.
    import tempfile
    folder = tempfile.mkdtemp(prefix="upd")
    path = os.path.join(folder, "u.sock")
    monkeypatch.setattr(updater_client, "SOCKET_PATH", path)
    yield path
    if os.path.exists(path):
        os.unlink(path)
    os.rmdir(folder)


@pytest.fixture
def running_service(docker, socket_path):
    updater = service.Updater()
    server = service.serve(socket_path, updater)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield updater
    server.shutdown()
    server.server_close()


def test_no_service_is_an_ordinary_answer(socket_path):
    assert updater_client.status() is None


def test_a_socket_nobody_listens_on_is_no_service(socket_path):
    import socket
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(socket_path)
    stale.close()

    assert updater_client.status() is None


def test_the_socket_can_be_reached_by_any_uid(running_service, socket_path):
    assert os.stat(socket_path).st_mode & 0o777 == 0o666


def test_status_and_start_over_the_socket(running_service, docker):
    assert updater_client.status()["ready"] is True

    status, payload = updater_client.start()

    assert status == 202
    deadline = time.time() + 3
    while running_service.state != "done" and time.time() < deadline:
        time.sleep(0.01)
    assert running_service.state == "done"


def test_the_service_takes_no_other_verb(running_service, docker):
    status, _ = updater_client._call("POST", "/update/../exec")

    assert status == 404
    assert docker.compose_verbs == []


# ── The panel's endpoint ─────────────────────────────────────────────────────


@pytest.fixture
def update_published(monkeypatch):
    monkeypatch.setattr(updates, "_releases", [{"version": "9.0.0"}])


def test_the_status_reports_the_service(client, running_service):
    enable_open_mode()

    assert client.get("/api/updates").json()["updater"]["ready"] is True


def test_the_status_says_there_is_no_service(client, socket_path):
    enable_open_mode()

    assert client.get("/api/updates").json()["updater"] is None


def test_install_with_nothing_new_cannot_restart_the_panel(client, running_service, docker, monkeypatch):
    """In open mode any visitor reaches this."""
    enable_open_mode()
    monkeypatch.setattr(updates, "_releases", [])

    assert client.post("/api/updates/install").status_code == 409
    assert docker.compose_verbs == []


@pytest.mark.parametrize("status", ["running", "queued", "paused"])
def test_install_refuses_while_downloads_would_be_lost(
    client, running_service, docker, update_published, monkeypatch, status
):
    enable_open_mode()
    monkeypatch.setattr(job_manager, "list_jobs", lambda: [{"status": status}])

    response = client.post("/api/updates/install")

    assert response.status_code == 409
    assert "1 download" in response.json()["detail"]
    assert docker.compose_verbs == []


def test_finished_and_scheduled_jobs_do_not_block_an_update(
    client, running_service, update_published, monkeypatch
):
    enable_open_mode()
    monkeypatch.setattr(
        job_manager, "list_jobs",
        lambda: [{"status": s} for s in ("done", "failed", "cancelled", "scheduled")],
    )

    assert client.post("/api/updates/install").status_code == 202


def test_install_without_the_service(client, socket_path, update_published):
    enable_open_mode()

    assert client.post("/api/updates/install").status_code == 503


def test_install_reports_why_the_service_is_not_ready(client, running_service, docker, update_published):
    enable_open_mode()
    os.unlink(docker.compose_file)

    response = client.post("/api/updates/install")

    assert response.status_code == 503
    assert docker.compose_file in response.json()["detail"]


def test_install_copies_the_database_first(client, running_service, update_published):
    from app import db
    enable_open_mode()

    assert client.post("/api/updates/install").status_code == 202

    assert os.path.getsize(f"{db.current_path()}.pre-update") > 0


def test_no_copy_of_the_database_no_update(client, running_service, docker, update_published, monkeypatch):
    from app.routers import updates as updates_router
    enable_open_mode()

    def refuse():
        raise OSError("disk full")

    monkeypatch.setattr(updates_router, "_backup_database", refuse)

    assert client.post("/api/updates/install").status_code == 500
    assert docker.compose_verbs == []
