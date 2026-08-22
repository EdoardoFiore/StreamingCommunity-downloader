"""The source domain is recovered, not guessed.

Two things are under test here and they pull in opposite directions. The panel
has to find the new domain on its own, because the alternative is that it stays
broken until someone notices. And it has to refuse almost everything it finds,
because the page it reads is edited by people we do not control and the domain
decides where every outbound request goes.

The rejection cases are therefore the important ones, in particular
``streamingcommunity.attacker.tld``: it passes any check that looks only at the
first label, while the part that actually decides where the traffic lands
belongs to somebody else.
"""

import json

import pytest

from app import config
from app.auth.permissions import ALL_PERMISSIONS
from app.core import domain_recovery
from tests.conftest import do_setup, make_user, session_for


PAGE = """
<html><body>
  <h1>Link Aggiornato StreamingCommunity</h1>
  <p>Se il sito non funziona bisogna cambiare dns!</p>
  <p><a href="https://streamingcommunityz.rodeo/">https://streamingcommunityz.rodeo/</a></p>
  <p><a href="https://telegra.ph/api">telegra.ph</a></p>
</body></html>
"""


class _FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _reset_module_state():
    """The pending candidate and the throttle are process-wide by design."""
    domain_recovery.clear_pending()
    domain_recovery._last_check_at = None
    yield
    domain_recovery.clear_pending()
    domain_recovery._last_check_at = None


@pytest.fixture
def public_dns(monkeypatch):
    """Every candidate resolves to a public address unless a test says otherwise."""
    monkeypatch.setattr(
        domain_recovery.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 443))],
    )


@pytest.fixture
def page(monkeypatch):
    """Serve the candidates page, and record what was requested."""
    calls = []

    def fake_get(url, *args, **kwargs):
        calls.append((url, kwargs))
        return _FakeResponse(PAGE)

    monkeypatch.setattr(domain_recovery.requests, "get", fake_get)
    return calls


# ── Reading the page ──────────────────────────────────────────────────────────

def test_candidates_are_the_https_hosts_on_the_page(page):
    assert domain_recovery.fetch_candidates() == ["streamingcommunityz.rodeo", "telegra.ph"]


def test_reading_the_page_has_a_timeout(page):
    domain_recovery.fetch_candidates()
    assert page[-1][1].get("timeout") is not None


def test_a_page_that_errors_does_not_produce_candidates(monkeypatch):
    monkeypatch.setattr(
        domain_recovery.requests, "get", lambda *a, **k: _FakeResponse("", 503)
    )
    with pytest.raises(RuntimeError):
        domain_recovery.fetch_candidates()


# ── The guard ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "host",
    [
        "streamingcommunityz.rodeo",
        "streamingcommunity.bet",
        "www.streamingcommunityz.rodeo",
        "animeunity.so".replace("anime", "streaming"),  # streamingunity.so
    ],
)
def test_plausible_hosts_are_accepted(host, public_dns):
    ok, reason = domain_recovery.is_plausible(host)
    assert ok, reason


@pytest.mark.parametrize(
    "host, expected_in_reason",
    [
        # The one that matters: first-label-only checks would let this through,
        # and the host that receives the traffic is attacker.tld.
        ("streamingcommunity.attacker.tld", "secondo livello"),
        ("evil.example", "non riconosciuto"),
        ("streamingcommunity.evil.co.uk", "secondo livello"),
        ("localhost", "locale"),
        ("streamingcommunity.local", "locale"),
        ("192.168.1.10", "IP"),
        ("a.b", "forma non valida"),
        ("streaming community.bet", "forma non valida"),
    ],
)
def test_implausible_hosts_are_rejected(host, expected_in_reason, public_dns):
    ok, reason = domain_recovery.is_plausible(host)
    assert not ok
    assert expected_in_reason in reason


def test_a_host_resolving_inside_the_network_is_rejected(monkeypatch):
    monkeypatch.setattr(
        domain_recovery.socket,
        "getaddrinfo",
        lambda *a, **k: [(2, 1, 6, "", ("10.0.0.1", 443))],
    )
    ok, reason = domain_recovery.is_plausible("streamingcommunityz.rodeo")
    assert not ok
    assert "non pubblico" in reason


def test_a_host_that_does_not_resolve_is_rejected(monkeypatch):
    def boom(*a, **k):
        raise OSError("NXDOMAIN")

    monkeypatch.setattr(domain_recovery.socket, "getaddrinfo", boom)
    ok, _ = domain_recovery.is_plausible("streamingcommunityz.rodeo")
    assert not ok


def test_an_http_link_is_not_a_candidate(monkeypatch):
    monkeypatch.setattr(
        domain_recovery.requests,
        "get",
        lambda *a, **k: _FakeResponse(
            '<a href="http://streamingcommunityz.rodeo/">x</a>'
        ),
    )
    assert domain_recovery.fetch_candidates() == []


# ── Verification ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("version", ["", None])
def test_an_empty_version_is_not_proof_of_anything(monkeypatch, version):
    """Stricter than PUT /api/domain on purpose — see verify()'s docstring."""
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: version)
    assert domain_recovery.verify("streamingcommunityz.rodeo") is None


def test_a_host_that_raises_is_not_verified(monkeypatch):
    def boom(host):
        raise RuntimeError("Cannot reach")

    monkeypatch.setattr(domain_recovery, "get_domain_version", boom)
    assert domain_recovery.verify("streamingcommunityz.rodeo") is None


# ── The cycle ─────────────────────────────────────────────────────────────────

@pytest.fixture
def broken_source(monkeypatch, _configured_domain, page, public_dns):
    """The configured domain is dead; the page offers a working replacement."""
    def version(host):
        if host == "streamingcommunityz.rodeo":
            return "v9"
        raise RuntimeError("Cannot reach")

    monkeypatch.setattr(domain_recovery, "get_domain_version", version)
    return _configured_domain


def _stored(data_file):
    return json.loads(data_file.read_text(encoding="utf-8"))


def test_a_candidate_is_proposed_and_not_written(broken_source):
    result = domain_recovery.run_check()

    assert result["candidate"] == "streamingcommunityz.rodeo"
    assert result["applied"] is False
    assert domain_recovery.pending()["host"] == "streamingcommunityz.rodeo"
    assert _stored(broken_source)["domain"] == "example.test"


def test_auto_apply_writes_the_domain(broken_source):
    config.save_settings({**config.get_settings(), "domain_auto_apply": True})

    result = domain_recovery.run_check()

    assert result["applied"] is True
    assert _stored(broken_source)["domain"] == "streamingcommunityz.rodeo"
    assert domain_recovery.pending() is None


def test_rejected_candidates_are_reported(monkeypatch, broken_source):
    """What was refused has to be visible: a rebrand looks exactly like an attack.

    Only candidates ahead of the winning one are reported — the scan stops at
    the first host that verifies rather than making a network round trip per
    remaining link.
    """
    monkeypatch.setattr(
        domain_recovery.requests,
        "get",
        lambda *a, **k: _FakeResponse(
            '<a href="https://telegra.ph/api">x</a>'
            '<a href="https://streamingcommunityz.rodeo/">y</a>'
        ),
    )

    result = domain_recovery.run_check()

    assert result["candidate"] == "streamingcommunityz.rodeo"
    assert [r["host"] for r in result["rejected"]] == ["telegra.ph"]


def test_a_working_domain_is_left_alone(monkeypatch, _configured_domain, page, public_dns):
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: "v1")

    result = domain_recovery.run_check()

    assert result["current_ok"] is True
    assert result["candidate"] is None
    # The page is somebody else's server: a healthy panel must not touch it.
    assert page == []


def test_forcing_looks_even_when_the_domain_works(monkeypatch, _configured_domain, page, public_dns):
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: "v1")

    result = domain_recovery.run_check(force=True)

    assert result["current_ok"] is True
    assert result["candidate"] == "streamingcommunityz.rodeo"


def test_the_throttle_holds_between_checks(broken_source):
    domain_recovery.run_check()
    second = domain_recovery.run_check()

    assert second["checked"] is False


def test_forcing_bypasses_the_throttle(broken_source):
    domain_recovery.run_check()
    assert domain_recovery.run_check(force=True)["checked"] is True


def test_the_throttle_expires(monkeypatch, broken_source):
    clock = [1000.0]
    monkeypatch.setattr(domain_recovery, "_now", lambda: clock[0])

    domain_recovery.run_check()
    clock[0] += domain_recovery._MIN_CHECK_INTERVAL + 1

    assert domain_recovery.run_check()["checked"] is True


def test_an_unreachable_page_is_not_fatal(monkeypatch, _configured_domain):
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: None)

    def boom(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(domain_recovery.requests, "get", boom)

    result = domain_recovery.run_check()
    assert result["candidate"] is None
    assert domain_recovery.pending() is None


# ── Applying ──────────────────────────────────────────────────────────────────

def test_applying_reverifies_before_writing(monkeypatch, _configured_domain, public_dns):
    """A candidate can go stale between being proposed and being confirmed."""
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: None)

    with pytest.raises(RuntimeError):
        domain_recovery.apply_candidate("streamingcommunityz.rodeo")

    assert _stored(_configured_domain)["domain"] == "example.test"


def test_applying_refuses_an_implausible_host(monkeypatch, _configured_domain, public_dns):
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: "v9")

    with pytest.raises(RuntimeError):
        domain_recovery.apply_candidate("streamingcommunity.attacker.tld")

    assert _stored(_configured_domain)["domain"] == "example.test"


def test_applying_keeps_the_rest_of_data_json(monkeypatch, _configured_domain, public_dns):
    config.update_data({"libraries": [{"type": "film", "path": "/srv/films"}]})
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: "v9")

    domain_recovery.apply_candidate("streamingcommunityz.rodeo")

    stored = _stored(_configured_domain)
    assert stored["domain"] == "streamingcommunityz.rodeo"
    assert stored["libraries"] == [{"type": "film", "path": "/srv/films"}]


# ── Endpoints ─────────────────────────────────────────────────────────────────

def _csrf(admin):
    """The admin fixture hands back (user, csrf token)."""
    return {"X-CSRF-Token": admin[1]}


@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return user, session_for(client, user.id)


def test_the_candidate_endpoint_reports_the_pending_one(client, admin, monkeypatch, public_dns):
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: "v9")
    domain_recovery._set_pending({"host": "streamingcommunityz.rodeo", "version": "v9",
                                  "found_at": 0})

    body = client.get("/api/domain/candidate").json()
    assert body["candidate"]["host"] == "streamingcommunityz.rodeo"


def test_applying_without_a_candidate_is_a_conflict(client, admin):
    res = client.post("/api/domain/candidate/apply",
                      json={"domain": "streamingcommunityz.rodeo"}, headers=_csrf(admin))
    assert res.status_code == 409


def test_applying_a_different_host_than_the_one_found_is_refused(
    client, admin, monkeypatch, _configured_domain, public_dns
):
    """The body is a confirmation token, not an instruction.

    Honouring it would hand any settings manager the ability to point the panel
    at a host of their choosing — the thing every other endpoint here refuses to
    do.
    """
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: "v9")
    domain_recovery._set_pending({"host": "streamingcommunityz.rodeo", "version": "v9",
                                  "found_at": 0})

    res = client.post("/api/domain/candidate/apply",
                      json={"domain": "attacker.example"}, headers=_csrf(admin))

    assert res.status_code == 409
    assert _stored(_configured_domain)["domain"] == "example.test"


def test_applying_the_pending_candidate_writes_it(
    client, admin, monkeypatch, _configured_domain, public_dns
):
    monkeypatch.setattr(domain_recovery, "get_domain_version", lambda host: "v9")
    domain_recovery._set_pending({"host": "streamingcommunityz.rodeo", "version": "v9",
                                  "found_at": 0})

    res = client.post("/api/domain/candidate/apply",
                      json={"domain": "streamingcommunityz.rodeo"}, headers=_csrf(admin))

    assert res.status_code == 200
    assert _stored(_configured_domain)["domain"] == "streamingcommunityz.rodeo"
    assert domain_recovery.pending() is None


def test_dismissing_clears_the_candidate(client, admin):
    domain_recovery._set_pending({"host": "streamingcommunityz.rodeo", "version": "v9",
                                  "found_at": 0})
    assert client.post("/api/domain/candidate/dismiss",
                       headers=_csrf(admin)).status_code == 200
    assert domain_recovery.pending() is None


def test_the_recovery_settings_survive_a_settings_save(client, admin):
    """Every settings PUT rewrites the whole dict; the domain keys must ride along."""
    res = client.put("/api/domain/settings", headers=_csrf(admin), json={
        "max_concurrent_downloads": 4,
        "max_segment_workers": 8,
        "domain_auto_apply": True,
    })

    assert res.status_code == 200
    stored = config.get_settings()
    assert stored["domain_auto_apply"] is True
    assert stored["series_watch_interval_minutes"] == 240
    assert stored["max_concurrent_downloads"] == 4


def test_a_too_frequent_check_interval_is_refused(client, admin):
    res = client.put("/api/domain/settings", headers=_csrf(admin), json={
        "max_concurrent_downloads": 3,
        "max_segment_workers": 16,
        "domain_check_interval_minutes": 5,
    })
    assert res.status_code == 400
