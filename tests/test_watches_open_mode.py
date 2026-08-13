"""Following a series works without Jellyfin too.

With no accounts a watch belongs to the panel rather than to a user, and a new
episode downloads straight away: open mode has no queue to put it in and nobody
to approve it, which is how it treats every other download.
"""

import pytest

from app import main as main_module
from app.auth import deps
from app.auth import router as auth_router
from app.requests import models as request_models
from app.watches import models as watch_models, poller


@pytest.fixture
def open_panel(client, source, stub_jobs, monkeypatch):
    """A panel with no Jellyfin connection; the source has 3 episodes/1 season."""
    for module in (deps, auth_router, main_module):
        monkeypatch.setattr(module, "AUTH_ENABLED", False)
    from app.core import tv

    monkeypatch.setattr(tv, "get_info_tv", lambda *a, **k: 1)
    return source


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


def test_a_series_can_be_followed_without_accounts(client, open_panel):
    response = client.post("/api/watches", json=TV_BODY)

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["created_by"] is None
    assert body["followers"] == []
    # Seeding still happens, so following means "from here on".
    assert watch_models.seen_keys(body["id"]) == {"S01E1", "S01E2", "S01E3"}


def test_the_watch_list_shows_the_panel_s_own_watches(client, open_panel):
    client.post("/api/watches", json=TV_BODY)

    listed = client.get("/api/watches/mine").json()["watches"]

    assert [w["title"] for w in listed] == ["Test Series"]


def test_the_status_endpoint_reports_it_as_followed(client, open_panel):
    client.post("/api/watches", json=TV_BODY)
    params = {"source": "streamingcommunity", "media_type": "tv", "external_id": "77"}

    body = client.get("/api/watches/status", params=params).json()

    assert body["following"] is True
    assert body["followed_by_me"] is True


def test_a_new_episode_downloads_straight_away(client, open_panel, stub_jobs):
    client.post("/api/watches", json=TV_BODY)
    open_panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    result = poller.run_poll_cycle()

    assert result["new"] == 1
    assert [name for name, _, _ in stub_jobs] == ["submit_episode"]
    # No request is created: there is no queue and nobody to approve it.
    assert request_models.list_all() == []


def test_the_download_never_substitutes_an_audio_track(client, open_panel, stub_jobs):
    client.post("/api/watches", json=TV_BODY)
    open_panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    poller.run_poll_cycle()

    _, _, kwargs = stub_jobs[0]
    assert kwargs["strict_audio"] is True
    assert kwargs["user_id"] is None


def test_an_episode_already_in_the_library_is_left_alone(client, open_panel, stub_jobs):
    client.post("/api/watches", json=TV_BODY)
    open_panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})
    season = open_panel.library / "Test Series (2019)" / "Season 01"
    season.mkdir(parents=True)
    (season / "Test Series S01E04.mkv").write_text("x")

    result = poller.poll_watch(watch_models.list_all()[0])

    assert result["outcomes"] == {"already_in_library": 1}
    assert stub_jobs == []


def test_unfollowing_stops_the_watch(client, open_panel):
    watch_id = client.post("/api/watches", json=TV_BODY).json()["id"]

    response = client.delete(f"/api/watches/{watch_id}")

    assert response.status_code == 200
    assert watch_models.get(watch_id).enabled is False
    assert client.get("/api/watches/mine").json()["watches"] == []


def test_a_source_that_cannot_be_read_leaves_no_watch_behind(client, open_panel):
    open_panel.dead = True

    response = client.post("/api/watches", json=TV_BODY)

    assert response.status_code == 502
    assert watch_models.list_all() == []


def test_an_anime_is_followed_the_same_way(client, open_panel, stub_jobs):
    body = {
        "source": "animeunity", "media_type": "anime", "external_id": "55",
        "title": "Test Anime", "anime_type": "tv",
        "audio_languages": ["ita"], "subtitle_languages": [],
    }
    watch_id = client.post("/api/watches", json=body).json()["id"]
    assert watch_models.seen_keys(watch_id) == {"E1", "E2", "E3"}

    open_panel.anime_episodes.append({"id": 804, "number": "4"})
    poller.run_poll_cycle()

    assert [name for name, _, _ in stub_jobs] == ["submit_anime_episode"]
    assert request_models.list_all() == []


def test_a_failed_submission_does_not_stop_the_cycle(client, open_panel, monkeypatch):
    """The episode is still marked seen, so one broken episode is not retried
    forever at the expense of the rest."""
    from app.jobs import job_manager

    client.post("/api/watches", json=TV_BODY)
    open_panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    def explode(*args, **kwargs):
        raise RuntimeError("fonte irraggiungibile")

    monkeypatch.setattr(job_manager, "submit_episode", explode)

    watch = watch_models.list_all()[0]
    result = poller.poll_watch(watch)

    assert result["outcomes"] == {"submit_failed": 1}
    assert "S01E4" in watch_models.seen_keys(watch.id)


# ── The bell, without accounts ────────────────────────────────────────────────

def test_a_finished_download_reaches_the_bell(client, open_panel, stub_jobs):
    """Without accounts the bell holds the panel's own notifications: a download
    nobody requested used to have nowhere to be reported."""
    from app import downloads_notify
    from app.jobs import job_manager

    job = job_manager._make_job("Inception", "film", media_label="Inception",
                                year="2010", user_id=None)
    job.status = "done"
    downloads_notify.on_job_finished(job)

    body = client.get("/api/notifications").json()
    assert body["unread"] == 1
    assert "Inception (2010)" in body["items"][0]["message"]


def test_the_panel_bell_can_be_marked_read(client, open_panel, stub_jobs):
    from app import downloads_notify
    from app.jobs import job_manager

    job = job_manager._make_job("Film", "film", user_id=None)
    job.status = "done"
    downloads_notify.on_job_finished(job)

    assert client.post("/api/notifications/read", json={"ids": []}).json()["unread"] == 0
    assert client.get("/api/notifications").json()["unread"] == 0


def test_the_panel_bell_can_be_cleared(client, open_panel, stub_jobs):
    """Deleting has to reach the panel's own rows too, or the one bell that
    exists without accounts is the one that cannot be emptied."""
    from app import downloads_notify
    from app.jobs import job_manager

    for title in ("Film", "Altro Film"):
        job = job_manager._make_job(title, "film", user_id=None)
        job.status = "done"
        downloads_notify.on_job_finished(job)

    items = client.get("/api/notifications").json()["items"]
    assert len(items) == 2

    assert client.post("/api/notifications/delete",
                       json={"ids": [items[0]["id"]]}).json()["deleted"] == 1
    assert len(client.get("/api/notifications").json()["items"]) == 1

    assert client.post("/api/notifications/delete", json={}).json()["deleted"] == 1
    assert client.get("/api/notifications").json()["items"] == []


def test_an_event_with_no_recipients_is_not_broadcast(client, open_panel):
    """panel_wide is asked for explicitly. An event that merely found no
    approvers must stay unsent, not become everyone's notification."""
    from app.requests import notify

    notify.notify(notify.REQUEST_CREATED, "Nessun destinatario.", [])

    assert client.get("/api/notifications").json()["items"] == []


def test_checking_a_watch_by_hand_is_allowed_without_accounts(client, open_panel, stub_jobs):
    watch_id = client.post("/api/watches", json=TV_BODY).json()["id"]
    open_panel.episodes.append({"id": 904, "n": "4", "name": "Episodio 4"})

    response = client.post(f"/api/watches/{watch_id}/check")

    assert response.status_code == 200, response.text
    assert response.json()["new"] == 1
    assert [name for name, _, _ in stub_jobs] == ["submit_episode"]
