"""COOKIE_SAMESITE: the escape hatch for embedding the panel in an iframe on a
different site or scheme (e.g. a Jellyfin custom tab), where a SameSite=Lax
cookie is silently dropped and login looks like an infinite loop.
"""

import logging

from app.config import _resolve_samesite
from tests.conftest import do_setup


# ── Pure validation logic ────────────────────────────────────────────────────────

def test_default_is_lax():
    assert _resolve_samesite("lax", secure=False) == "lax"


def test_none_is_accepted_as_is_when_secure():
    assert _resolve_samesite("none", secure=True) == "none"


def test_strict_is_accepted():
    assert _resolve_samesite("strict", secure=True) == "strict"


def test_value_matching_is_case_and_whitespace_insensitive():
    assert _resolve_samesite("  None  ", secure=True) == "none"
    assert _resolve_samesite("STRICT", secure=True) == "strict"


def test_invalid_value_falls_back_to_lax_and_warns(caplog):
    with caplog.at_level(logging.WARNING):
        result = _resolve_samesite("bogus", secure=False)
    assert result == "lax"
    assert any("Invalid COOKIE_SAMESITE" in r.message for r in caplog.records)


def test_none_without_secure_still_resolves_to_none_but_warns(caplog):
    """The value is honoured (it's what the operator asked for) — a hard
    override here would just hide the actual misconfiguration."""
    with caplog.at_level(logging.WARNING):
        result = _resolve_samesite("none", secure=False)
    assert result == "none"
    assert any("requires COOKIE_SECURE=1" in r.message for r in caplog.records)


def test_none_with_secure_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        _resolve_samesite("none", secure=True)
    assert not any("requires COOKIE_SECURE=1" in r.message for r in caplog.records)


def test_lax_without_secure_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        _resolve_samesite("lax", secure=False)
    assert not caplog.records


# ── Wired into the actual session cookie ────────────────────────────────────────

def test_default_router_behaviour_sets_samesite_lax(client, admin_credentials):
    response = client.post("/api/auth/setup", json=admin_credentials)
    assert "samesite=lax" in response.headers.get("set-cookie", "").lower()


def test_router_honours_a_configured_samesite_none(client, admin_credentials, monkeypatch):
    from app.auth import router

    monkeypatch.setattr(router, "COOKIE_SAMESITE", "none")
    monkeypatch.setattr(router, "COOKIE_SECURE", True)

    response = client.post("/api/auth/setup", json=admin_credentials)

    cookie_header = response.headers.get("set-cookie", "").lower()
    assert "samesite=none" in cookie_header
    assert "secure" in cookie_header
