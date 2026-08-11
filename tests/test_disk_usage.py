"""Disk usage: reported per library path, on the mounted volume."""

import json

import pytest

from app.auth.permissions import ALL_PERMISSIONS, Permission
from app.routers import domain
from tests.conftest import do_setup, make_user, session_for


@pytest.fixture(autouse=True)
def _clear_cache():
    domain._disk_usage_cache["data"] = None
    yield
    domain._disk_usage_cache["data"] = None


@pytest.fixture
def libraries(tmp_path, monkeypatch):
    """Two real library paths on one volume, plus one that does not exist."""
    films = tmp_path / "films"
    series = tmp_path / "series"
    films.mkdir()
    series.mkdir()
    data_file = tmp_path / "data.json"
    data_file.write_text(json.dumps({
        "domain": "example.test",
        "libraries": [
            {"type": "film", "path": str(films)},
            {"type": "tv", "path": str(series)},
            {"type": "anime", "path": str(tmp_path / "missing")},
        ],
    }))
    monkeypatch.setattr(domain, "DATA_FILE", data_file)
    return films, series


@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    session_for(client, user.id)
    return user


def test_usage_is_reported_per_library(client, admin, libraries):
    response = client.get("/api/domain/disk-usage")

    assert response.status_code == 200
    entries = response.json()["libraries"]
    assert [e["type"] for e in entries] == ["film", "tv", "anime"]
    for entry in entries[:2]:
        assert entry["error"] is None
        assert entry["total"] > 0
        assert entry["used"] + entry["free"] <= entry["total"]


def test_libraries_on_one_volume_report_the_same_numbers(client, admin, libraries):
    entries = client.get("/api/domain/disk-usage").json()["libraries"]

    film, tv = entries[0], entries[1]
    assert (film["total"], film["free"]) == (tv["total"], tv["free"])


def test_an_unreadable_path_does_not_blank_out_the_others(client, admin, libraries):
    entries = client.get("/api/domain/disk-usage").json()["libraries"]

    missing = entries[2]
    assert missing["error"] is not None
    assert missing["total"] is None
    assert entries[0]["total"] > 0


def test_only_settings_managers_see_the_disk(client, admin_credentials, libraries):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", int(Permission.REQUEST))
    client.cookies.clear()
    session_for(client, bob.id)

    assert client.get("/api/domain/disk-usage").status_code == 403
