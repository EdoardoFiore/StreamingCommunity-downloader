"""External notification channels: CRUD, event filtering and delivery."""

import pytest

from app.auth.permissions import ALL_PERMISSIONS, Permission
from app.requests import apprise_channel, notify
from tests.conftest import do_setup, make_user, session_for


DISCORD_URL = "discord://123456789/abcdefghijklmnop"


@pytest.fixture
def admin(client, admin_credentials):
    do_setup(client, admin_credentials)
    user = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    return user, session_for(client, user.id)


@pytest.fixture
def sent(monkeypatch):
    """Record deliveries instead of reaching the network."""
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        apprise_channel, "send", lambda url, message, title=None: calls.append((url, message)) or True
    )
    monkeypatch.setattr(apprise_channel, "url_is_valid", lambda url: "://" in url)
    return calls


def test_create_list_and_delete_a_channel(client, admin, sent):
    _, csrf = admin
    headers = {"X-CSRF-Token": csrf}

    created = client.post(
        "/api/notification-channels",
        json={"name": "Discord admin", "apprise_url": DISCORD_URL},
        headers=headers,
    )
    assert created.status_code == 200, created.text
    channel_id = created.json()["id"]
    assert created.json()["enabled"] is True
    assert created.json()["events"] == []

    listed = client.get("/api/notification-channels")
    assert [c["name"] for c in listed.json()["channels"]] == ["Discord admin"]

    assert client.delete(f"/api/notification-channels/{channel_id}", headers=headers).status_code == 200
    assert client.get("/api/notification-channels").json()["channels"] == []


def test_an_unparseable_url_is_refused(client, admin, sent):
    _, csrf = admin
    response = client.post(
        "/api/notification-channels",
        json={"name": "rotto", "apprise_url": "non-una-url"},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 400
    assert client.get("/api/notification-channels").json()["channels"] == []


def test_only_settings_managers_reach_the_channels(client, admin_credentials, sent):
    do_setup(client, admin_credentials)
    bob = make_user("bob", "jf-bob-id", int(Permission.REQUEST))
    client.cookies.clear()
    session_for(client, bob.id)

    assert client.get("/api/notification-channels").status_code == 403


def test_notify_reaches_an_enabled_channel(client, admin, sent):
    user, _ = admin
    apprise_channel.create_channel(name="Discord", apprise_url=DISCORD_URL, events=[])

    notify.notify(notify.REQUEST_COMPLETED, "Film pronto.", [user.id])

    assert sent == [(DISCORD_URL, "Film pronto.")]


def test_a_disabled_channel_is_skipped(client, admin, sent):
    user, _ = admin
    channel = apprise_channel.create_channel(name="Discord", apprise_url=DISCORD_URL, events=[])
    apprise_channel.update_channel(channel["id"], enabled=False)

    notify.notify(notify.REQUEST_COMPLETED, "Film pronto.", [user.id])

    assert sent == []


def test_a_channel_only_gets_the_events_it_subscribed_to(client, admin, sent):
    user, _ = admin
    apprise_channel.create_channel(
        name="Solo completati", apprise_url=DISCORD_URL, events=[notify.REQUEST_COMPLETED]
    )

    notify.notify(notify.REQUEST_CREATED, "Richiesta creata.", [user.id])
    assert sent == []

    notify.notify(notify.REQUEST_COMPLETED, "Film pronto.", [user.id])
    assert [message for _, message in sent] == ["Film pronto."]


def test_a_failing_channel_does_not_break_the_notification(client, admin, monkeypatch):
    user, _ = admin

    def explode(*args, **kwargs):
        raise RuntimeError("webhook morto")

    monkeypatch.setattr(apprise_channel, "send", explode)
    apprise_channel.create_channel(name="Rotto", apprise_url=DISCORD_URL, events=[])

    # The in-app notification must still be stored: a dead webhook cannot be
    # allowed to swallow the user's own notification, let alone the download.
    notify.notify(notify.REQUEST_COMPLETED, "Film pronto.", [user.id])

    assert notify.unread_count(user.id) == 1
