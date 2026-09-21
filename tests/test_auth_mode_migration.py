"""Migration v8: an existing deployment keeps the mode it was already in.

Which mode the panel runs in moved from the ``AUTH_ENABLED`` environment
variable to the ``auth_mode`` setting the wizard writes. A database that
predates the move has no answer stored, so the variable is read one last time
here — and only for a database that already existed, since a fresh install
must always be asked.
"""

import pytest

from app import db
from app.auth import models


@pytest.fixture
def legacy_db(tmp_path):
    """A database stopped at v7: every table, no auth_mode row, no users.

    Built by running the migration list as it was before v8 existed, which is
    exactly the state an upgrading deployment's file is in. Restored by hand
    rather than with monkeypatch, whose undo() is shared with the autouse
    fixtures and would roll their patches back too.
    """
    db.configure(tmp_path / "legacy.db")
    full = db.MIGRATIONS
    db.MIGRATIONS = full[:7]
    try:
        db.run_migrations()
    finally:
        db.MIGRATIONS = full
    yield
    db.close_all()


def _auth_mode():
    return models.get_setting(models.SETTING_AUTH_MODE)


def test_existing_open_deployment_is_carried_over(legacy_db, monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)

    db.run_migrations()

    assert _auth_mode() == "open"
    assert models.setup_done() is True


def test_existing_deployment_that_asked_for_jellyfin_still_gets_the_wizard(
    legacy_db, monkeypatch
):
    """AUTH_ENABLED=1 with nobody configured is a panel whose owner wanted a
    login and never finished. Opening it here would be a downgrade nobody
    chose, so the wizard stays."""
    monkeypatch.setenv("AUTH_ENABLED", "1")

    db.run_migrations()

    assert _auth_mode() is None
    assert models.setup_done() is False


def test_existing_deployment_with_users_is_left_alone(legacy_db, monkeypatch):
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    db.execute(
        "INSERT INTO jf_user(jellyfin_user_id, username, device_id, created_at) "
        "VALUES('jf-admin', 'admin', 'dev', '2024-01-01T00:00:00Z')"
    )

    db.run_migrations()

    assert _auth_mode() is None


def test_existing_deployment_with_a_jellyfin_url_is_left_alone(legacy_db, monkeypatch):
    """Set up before SETTING_AUTH_MODE existed: a URL but no mode key. Those
    installs are already recognised by setup_done(), and must not be turned
    open behind the admin's back."""
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    models.set_setting(models.SETTING_JELLYFIN_URL, "http://jellyfin.local:8096")

    db.run_migrations()

    assert _auth_mode() is None


def test_a_fresh_database_is_never_carried_over(tmp_path, monkeypatch):
    """The variable has no say over a new install, even when it is set — the
    wizard asks."""
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    db.configure(tmp_path / "fresh.db")

    db.run_migrations()

    assert _auth_mode() is None
    db.close_all()


def test_the_backfill_runs_once_and_does_not_fight_a_later_choice(
    legacy_db, monkeypatch, tmp_path
):
    """Migrations are keyed on user_version, so a panel that later connects to
    Jellyfin is not pushed back to open on the next start."""
    monkeypatch.delenv("AUTH_ENABLED", raising=False)
    db.run_migrations()
    models.set_setting(models.SETTING_AUTH_MODE, "jellyfin")

    db.run_migrations()

    assert _auth_mode() == "jellyfin"
