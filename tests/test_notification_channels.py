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
    """Record deliveries instead of reaching the network.

    Takes **kw so that adding a presentation argument to send() shows up as a
    test that needs updating, not as a TypeError inside a swallowed exception.
    """
    calls: list[dict] = []

    def record(url, message, title=None, notify_type=None, **kw):
        calls.append({"url": url, "message": message, "title": title,
                      "notify_type": notify_type, **kw})
        return True

    monkeypatch.setattr(apprise_channel, "send", record)
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

    assert [(c["url"], c["message"]) for c in sent] == [(DISCORD_URL, "Film pronto.")]


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
    assert [c["message"] for c in sent] == ["Film pronto."]


def test_apprise_delivers_even_with_no_in_app_recipients(client, admin, sent):
    """External channels belong to the panel, not to a user.

    A direct download in open mode has no account to notify in the bell, which
    is precisely the case a Discord webhook exists to report.
    """
    apprise_channel.create_channel(name="Discord", apprise_url=DISCORD_URL, events=[])

    notify.notify(notify.REQUEST_COMPLETED, "Film pronto.", [])

    assert [c["message"] for c in sent] == ["Film pronto."]


def test_the_outcome_reaches_the_channel_as_a_colour(client, admin, sent):
    user, _ = admin
    apprise_channel.create_channel(name="Discord", apprise_url=DISCORD_URL, events=[])

    notify.notify(notify.REQUEST_COMPLETED, "Pronto.", [user.id])
    notify.notify(notify.REQUEST_FAILED, "Rotto.", [user.id])
    notify.notify(notify.REQUEST_NEEDS_ATTENTION, "Da guardare.", [user.id])

    assert [c["notify_type"] for c in sent] == ["success", "failure", "warning"]


def test_a_caller_can_override_the_outcome(client, admin, sent):
    user, _ = admin
    apprise_channel.create_channel(name="Discord", apprise_url=DISCORD_URL, events=[])

    notify.notify(notify.REQUEST_COMPLETED, "Parziale.", [user.id],
                  notify_type=notify.WARNING)

    assert sent[0]["notify_type"] == "warning"


def test_external_channels_get_the_markdown_text(client, admin, sent):
    user, _ = admin
    apprise_channel.create_channel(name="Discord", apprise_url=DISCORD_URL, events=[])

    notify.notify(notify.REQUEST_COMPLETED, "Film pronto.", [user.id],
                  markdown_message="«**Film**» è pronto.", title="Download completato")

    assert sent[0]["message"] == "«**Film**» è pronto."
    assert sent[0]["title"] == "Download completato"
    # The bell keeps the plain wording, without the markup.
    assert notify.list_for_user(user.id)[0]["message"] == "Film pronto."


def test_an_unknown_event_name_is_refused(client, admin, sent):
    """A typo used to become a filter that silently never matched."""
    _, csrf = admin
    headers = {"X-CSRF-Token": csrf}

    created = client.post(
        "/api/notification-channels",
        json={"name": "x", "apprise_url": DISCORD_URL, "events": ["request_inventato"]},
        headers=headers,
    )
    assert created.status_code == 422

    real = client.post(
        "/api/notification-channels",
        json={"name": "x", "apprise_url": DISCORD_URL, "events": [notify.REQUEST_COMPLETED]},
        headers=headers,
    ).json()
    patched = client.patch(
        f"/api/notification-channels/{real['id']}",
        json={"events": ["non_esiste"]},
        headers=headers,
    )
    assert patched.status_code == 422


def test_events_are_deduplicated_and_ordered(client, admin, sent):
    _, csrf = admin

    created = client.post(
        "/api/notification-channels",
        json={
            "name": "x", "apprise_url": DISCORD_URL,
            "events": [notify.DOWNLOAD_FAILED, notify.REQUEST_CREATED, notify.DOWNLOAD_FAILED],
        },
        headers={"X-CSRF-Token": csrf},
    ).json()

    assert created["events"] == [notify.REQUEST_CREATED, notify.DOWNLOAD_FAILED]


def test_a_channel_subscribed_to_downloads_ignores_requests(client, admin, sent):
    user, _ = admin
    apprise_channel.create_channel(
        name="Solo download", apprise_url=DISCORD_URL,
        events=[notify.DOWNLOAD_BATCH_COMPLETED],
    )

    notify.notify(notify.REQUEST_COMPLETED, "Richiesta pronta.", [user.id])
    assert sent == []

    notify.notify(notify.DOWNLOAD_BATCH_COMPLETED, "Stagione pronta.", [user.id])
    assert [c["message"] for c in sent] == ["Stagione pronta."]


class TestDecoratedUrl:
    """Discord only renders a card when the URL asks for markdown."""

    def test_a_discord_url_gains_the_embed_parameters(self):
        decorated = apprise_channel._decorated_url(DISCORD_URL)
        assert "format=markdown" in decorated
        assert "fields=no" in decorated
        assert decorated.startswith(DISCORD_URL)

    def test_an_explicit_choice_is_left_alone(self):
        chosen = DISCORD_URL + "?format=text"
        decorated = apprise_channel._decorated_url(chosen)
        assert "format=markdown" not in decorated
        assert "format=text" in decorated

    def test_a_discord_webhook_url_is_recognised(self):
        raw = "https://discord.com/api/webhooks/123/abc"
        assert "format=markdown" in apprise_channel._decorated_url(raw)

    def test_other_services_are_untouched(self):
        for url in ("tgram://token/12345", "ntfy://host/topic", "mailto://user:pw@host"):
            assert apprise_channel._decorated_url(url) == url

    def test_a_malformed_url_is_returned_unchanged(self):
        assert apprise_channel._decorated_url("non-una-url") == "non-una-url"


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


def test_the_second_half_of_a_pair_stays_off_the_webhook(client, admin, sent):
    """Subscribers and approvers are two audiences on the bell but one webhook.

    A failed download tells both, in different words. On Discord that arrived
    twice, saying the same thing — so the second call is delivered in-app only.
    """
    user, _ = admin
    apprise_channel.create_channel(name="Discord", apprise_url=DISCORD_URL, events=[])

    notify.notify(notify.REQUEST_FAILED, "Prima metà.", [user.id])
    notify.notify(notify.REQUEST_FAILED, "Seconda metà.", [user.id], external=False)

    assert [c["message"] for c in sent] == ["Prima metà."]
    # Both still reach the bell: on the bell they are different audiences.
    assert notify.unread_count(user.id) == 2
