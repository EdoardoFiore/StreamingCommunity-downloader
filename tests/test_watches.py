"""Followed series: the follow endpoints, the diff and the auto-download rule."""

import pytest

from app.auth.permissions import ALL_PERMISSIONS, Permission
from app.requests import models as request_models, service
from app.watches import models as watch_models, poller
from tests.conftest import do_setup, make_user, session_for


TV_BODY = {
    "source": "streamingcommunity",
    "media_type": "tv",
    "external_id": "77",
    "slug": "test-series",
    "title": "Test Series",
    "year": "2019",
    "audio_languages": ["ita"],
    "subtitle_languages": [],
}

ANIME_BODY = {
    "source": "animeunity",
    "media_type": "anime",
    "external_id": "55",
    "title": "Test Anime",
    "anime_type": "tv",
    "audio_languages": ["ita"],
    "subtitle_languages": [],
}


@pytest.fixture
def panel(client, admin_credentials, source, stub_jobs, monkeypatch):
    """Configured panel whose source publishes 3 episodes / 1 season."""
    do_setup(client, admin_credentials)
    from app.core import tv

    monkeypatch.setattr(tv, "get_info_tv", lambda *a, **k: 1)
    return source


def _user(username, permissions):
    from app.auth import models as auth_models

    existing = auth_models.get_user_by_jellyfin_id(f"jf-{username}-id")
    return existing or make_user(username, f"jf-{username}-id", int(permissions))


def _login(client, user):
    client.cookies.clear()
    return session_for(client, user.id)


def _follow(client, csrf, body=TV_BODY):
    return client.post("/api/watches", json=body, headers={"X-CSRF-Token": csrf})


# ── Following ──────────────────────────────────────────────────────────────────

def test_following_a_series_seeds_the_existing_episodes(client, panel):
    bob = _user("bob", Permission.REQUEST)
    csrf = _login(client, bob)

    response = _follow(client, csrf)

    assert response.status_code == 201, response.text
    watch_id = response.json()["id"]
    # Everything already published counts as handled, so nothing is queued.
    assert watch_models.seen_keys(watch_id) == {"S01E1", "S01E2", "S01E3"}
    assert request_models.list_all() == []


def test_a_film_cannot_be_followed(client, panel):
    bob = _user("bob", Permission.REQUEST)
    csrf = _login(client, bob)

    response = _follow(client, csrf, {**TV_BODY, "media_type": "film"})

    assert response.status_code == 400


def test_a_second_follower_joins_the_same_watch(client, panel):
    bob = _user("bob", Permission.REQUEST)
    first = _follow(client, _login(client, bob), ).json()

    ann = _user("ann", Permission.REQUEST)
    second = _follow(client, _login(client, ann)).json()

    assert second["id"] == first["id"]
    assert sorted(second["followers"]) == ["ann", "bob"]


def test_unfollowing_the_last_follower_stops_the_watch(client, panel):
    bob = _user("bob", Permission.REQUEST)
    csrf = _login(client, bob)
    watch_id = _follow(client, csrf).json()["id"]

    response = client.delete(f"/api/watches/{watch_id}", headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200
    assert response.json()["stopped"] is True
    assert watch_models.get(watch_id).enabled is False
    assert client.get("/api/watches/mine").json()["watches"] == []


def test_a_source_that_cannot_be_read_leaves_no_watch_behind(client, panel):
    panel.dead = True
    bob = _user("bob", Permission.REQUEST)
    csrf = _login(client, bob)

    response = _follow(client, csrf)

    # Arming a watch with no baseline would queue the whole back catalogue on
    # the next cycle, so the follow is rolled back instead.
    assert response.status_code == 502
    assert watch_models.list_all() == []


def test_status_reports_whether_the_caller_follows_it(client, panel):
    bob = _user("bob", Permission.REQUEST)
    csrf = _login(client, bob)
    params = {"source": "streamingcommunity", "media_type": "tv", "external_id": "77"}

    assert client.get("/api/watches/status", params=params).json()["following"] is False

    _follow(client, csrf)
    body = client.get("/api/watches/status", params=params).json()
    assert body["following"] is True and body["followed_by_me"] is True


# ── The poll ───────────────────────────────────────────────────────────────────

def test_a_new_episode_becomes_a_pending_request(client, panel):
    bob = _user("bob", Permission.REQUEST)
    _follow(client, _login(client, bob))
    panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    result = poller.run_poll_cycle()

    assert result["new"] == 1
    created = request_models.list_all()
    assert len(created) == 1
    assert created[0].status == request_models.PENDING
    assert created[0].episode_number == "4"
    assert created[0].watch_id is not None


def test_an_owner_with_download_permission_skips_the_queue(client, panel, stub_jobs):
    ann = _user("ann", Permission.REQUEST | Permission.DOWNLOAD)
    _follow(client, _login(client, ann))
    panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    poller.run_poll_cycle()

    created = request_models.list_all()[0]
    assert created.status == request_models.DOWNLOADING
    assert stub_jobs, "the download must actually have been submitted"


def test_approving_once_makes_the_rest_of_the_series_automatic(client, panel, stub_jobs):
    bob = _user("bob", Permission.REQUEST)
    _follow(client, _login(client, bob))
    panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})
    poller.run_poll_cycle()
    pending = request_models.list_all()[0]

    boss = _user("boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    response = client.post(
        "/api/requests/approve-batch",
        json={"ids": [pending.id], "auto_approve_watch_ids": [pending.id]},
        headers={"X-CSRF-Token": csrf},
    )
    assert response.status_code == 202, response.text
    assert watch_models.get(pending.watch_id).auto_approve is True

    panel.episodes.append({"id": 905, "n": "5", "name": "Episodio 5"})
    poller.run_poll_cycle()

    latest = [r for r in request_models.list_all() if r.episode_number == "5"][0]
    assert latest.status == request_models.DOWNLOADING


def test_an_episode_already_in_the_library_is_not_requested(client, panel):
    ann = _user("ann", Permission.REQUEST | Permission.DOWNLOAD)
    _follow(client, _login(client, ann))
    panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})
    season = panel.library / "Test Series (2019)" / "Season 01"
    season.mkdir(parents=True)
    # .mkv, the form a multi-audio download is remuxed into.
    (season / "Test Series S01E04.mkv").write_text("x")

    result = poller.run_poll_cycle()

    assert result["new"] == 1
    assert request_models.list_all() == []


def test_revoking_download_sends_the_series_back_to_the_queue(client, panel):
    from app.auth import models as auth_models

    ann = _user("ann", Permission.REQUEST | Permission.DOWNLOAD)
    _follow(client, _login(client, ann))
    auth_models.set_permissions(ann.id, int(Permission.REQUEST))
    panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    poller.run_poll_cycle()

    assert request_models.list_all()[0].status == request_models.PENDING


def test_an_unchanged_series_creates_nothing(client, panel):
    bob = _user("bob", Permission.REQUEST)
    _follow(client, _login(client, bob))

    assert poller.run_poll_cycle() == {"watches": 1, "new": 0}
    assert request_models.list_all() == []


def test_anime_are_followed_the_same_way(client, panel, stub_jobs):
    ann = _user("ann", Permission.REQUEST | Permission.DOWNLOAD)
    csrf = _login(client, ann)
    watch_id = _follow(client, csrf, ANIME_BODY).json()["id"]
    assert watch_models.seen_keys(watch_id) == {"E1", "E2", "E3"}

    panel.anime_episodes.append({"id": 804, "number": "4"})
    poller.run_poll_cycle()

    created = request_models.list_all()[0]
    assert created.media_type == "anime"
    assert created.episode_number == "4"
    assert created.status == request_models.DOWNLOADING


def test_one_broken_series_does_not_stop_the_others(client, panel, monkeypatch):
    bob = _user("bob", Permission.REQUEST)
    csrf = _login(client, bob)
    _follow(client, csrf)
    _follow(client, csrf, ANIME_BODY)
    panel.anime_episodes.append({"id": 804, "number": "4"})

    real = poller.current_episodes

    def explode_on_tv(watch):
        if watch.media_type == "tv":
            raise RuntimeError("fonte irraggiungibile")
        return real(watch)

    monkeypatch.setattr(poller, "current_episodes", explode_on_tv)
    result = poller.run_poll_cycle()

    assert result["watches"] == 2
    assert [r.media_type for r in request_models.list_all()] == ["anime"]


# ── Checking a watch by hand ───────────────────────────────────────────────────

def test_the_approvers_bell_gets_a_followed_episode(client, panel):
    """The half that was in doubt. A follower without DOWNLOAD produces a
    pending request, and an approver has to be told it is there — otherwise it
    sits in a queue nobody knows to look at."""
    boss = _user("boss", ALL_PERMISSIONS)
    bob = _user("bob", Permission.REQUEST)
    _follow(client, _login(client, bob))
    panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    poller.run_poll_cycle()

    from app.requests import notify

    assert boss.id in notify.approver_ids()
    messages = [n["message"] for n in notify.list_for_user(boss.id)]
    assert any("Test Series S01E4" in m for m in messages), messages


def test_a_follower_can_check_their_own_watch(client, panel):
    """Following seeds every episode already published, so nothing happens until
    the source releases the next one — which can be weeks. Without this, a user
    who can only make requests had no way to see their watch do anything."""
    bob = _user("bob", Permission.REQUEST)
    csrf = _login(client, bob)
    watch_id = _follow(client, csrf).json()["id"]
    panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    response = client.post(f"/api/watches/{watch_id}/check",
                           headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200, response.text
    assert response.json()["new"] == 1
    created = request_models.list_all()
    assert [r.status for r in created] == [request_models.PENDING]


def test_checking_someone_elses_watch_still_needs_the_permission(client, panel):
    ann = _user("ann", Permission.REQUEST)
    watch_id = _follow(client, _login(client, ann)).json()["id"]

    bob = _user("bob", Permission.REQUEST)
    csrf = _login(client, bob)
    response = client.post(f"/api/watches/{watch_id}/check",
                           headers={"X-CSRF-Token": csrf})

    assert response.status_code == 403


def test_an_approver_can_check_a_watch_they_do_not_follow(client, panel):
    ann = _user("ann", Permission.REQUEST)
    watch_id = _follow(client, _login(client, ann)).json()["id"]

    boss = _user("boss", ALL_PERMISSIONS)
    csrf = _login(client, boss)
    response = client.post(f"/api/watches/{watch_id}/check",
                           headers={"X-CSRF-Token": csrf})

    assert response.status_code == 200, response.text
