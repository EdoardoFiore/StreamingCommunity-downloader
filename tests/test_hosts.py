"""AnimeUnity's host and the video host, settable from Settings › Sorgente.

Both moved TLD (animeunity.so → .to, vixcloud.co → .cc). The video host follows
the embed URL on its own; these pin the manual escape hatch for both.
"""

import pytest

from app import config
from app.core import _shared, animeunity, hosts
from app.auth.permissions import ALL_PERMISSIONS, Permission
from tests.conftest import do_setup, make_user, session_for


@pytest.fixture(autouse=True)
def _no_env_override(monkeypatch):
    monkeypatch.delenv("ANIMEUNITY_HOST", raising=False)


# ── Hostname validation ───────────────────────────────────────────────────────

@pytest.mark.parametrize("value,expected", [
    ("www.animeunity.to", "www.animeunity.to"),
    ("  WWW.AnimeUnity.TO  ", "www.animeunity.to"),
    ("https://www.animeunity.to/", "www.animeunity.to"),
    ("vixcloud.cc/", "vixcloud.cc"),
])
def test_a_pasted_host_is_reduced_to_its_name(value, expected):
    assert hosts.normalize_host(value) == expected


@pytest.mark.parametrize("value", [
    "", "localhost", "127.0.0.1", "10.0.0.1", "https://vixcloud.cc/storage/enc.key",
    "https://vixcloud.cc:8443/", "ftp://vixcloud.cc", "vixcloud.cc/embed", "a b.cc",
])
def test_anything_but_a_hostname_is_refused(value):
    with pytest.raises(ValueError):
        hosts.normalize_host(value)


# ── Resolution order ──────────────────────────────────────────────────────────

def test_animeunity_defaults_then_env_then_saved(monkeypatch):
    assert animeunity.host() == config.ANIMEUNITY_DEFAULT_HOST == "www.animeunity.to"

    monkeypatch.setenv("ANIMEUNITY_HOST", "www.animeunity.env")
    assert animeunity.host() == "www.animeunity.env"

    config.update_data({"animeunity_domain": "www.animeunity.saved"})
    assert animeunity.host() == "www.animeunity.saved"


def test_without_an_override_the_embed_url_is_untouched():
    url = "https://vixcloud.cc/embed/42?token=t&expires=9"
    assert _shared.apply_video_host(url) == url


def test_an_override_moves_the_embed_and_everything_built_from_it():
    config.update_data({"video_host": "vixcloud.new"})
    url = _shared.apply_video_host("https://vixcloud.co/embed/42?token=t&expires=9")
    assert url == "https://vixcloud.new/embed/42?token=t&expires=9"

    script = "window.video = {id: '42'}; window.masterPlaylist = {params: {'token': 't', 'expires': '9',}}"
    video, params = _shared._parse_content(script, url)
    assert _shared._get_m3u8_url(video, params).startswith("https://vixcloud.new/playlist/42?")


# ── API ───────────────────────────────────────────────────────────────────────

@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return {"X-CSRF-Token": session_for(client, user.id)}


@pytest.fixture
def verified(monkeypatch):
    seen = []
    monkeypatch.setattr(hosts, "verify_animeunity", lambda h: seen.append(("au", h)))
    monkeypatch.setattr(hosts, "verify_video_host", lambda h: seen.append(("video", h)))
    return seen


def test_hosts_round_trip_through_settings(client, admin, verified):
    res = client.put("/api/domain/hosts", headers=admin, json={
        "animeunity": "https://www.animeunity.xyz/", "video_host": "vixcloud.cc"})
    assert res.status_code == 200
    assert verified == [("au", "www.animeunity.xyz"), ("video", "vixcloud.cc")]

    body = client.get("/api/domain/hosts").json()
    assert body["animeunity"] == body["animeunity_saved"] == "www.animeunity.xyz"
    assert body["video_host"] == "vixcloud.cc"
    assert animeunity.host() == "www.animeunity.xyz"


def test_a_blank_field_resets_to_automatic(client, admin, verified):
    config.update_data({"animeunity_domain": "www.animeunity.xyz", "video_host": "vixcloud.cc"})
    res = client.put("/api/domain/hosts", headers=admin,
                     json={"animeunity": "", "video_host": ""})
    assert res.status_code == 200
    assert verified == []
    assert res.json()["animeunity"] == config.ANIMEUNITY_DEFAULT_HOST
    assert res.json()["video_host"] == ""


def test_a_host_that_fails_verification_is_not_stored(client, admin, monkeypatch):
    def refuse(host):
        raise RuntimeError(f"{host} non raggiungibile")

    monkeypatch.setattr(hosts, "verify_video_host", refuse)
    res = client.put("/api/domain/hosts", headers=admin, json={"video_host": "vixcloud.dead"})
    assert res.status_code == 400
    assert "non raggiungibile" in res.json()["detail"]
    assert config.configured_video_host() == ""


def test_an_invalid_host_is_refused_before_any_request(client, admin, verified):
    res = client.put("/api/domain/hosts", headers=admin, json={"video_host": "127.0.0.1"})
    assert res.status_code == 400
    assert verified == []


def test_changing_hosts_needs_manage_settings(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("plain", "jf-plain-id", int(Permission.DOWNLOAD))
    client.cookies.clear()
    headers = {"X-CSRF-Token": session_for(client, user.id)}
    assert client.get("/api/domain/hosts").status_code == 403
    assert client.put("/api/domain/hosts", headers=headers,
                      json={"video_host": "vixcloud.cc"}).status_code == 403
