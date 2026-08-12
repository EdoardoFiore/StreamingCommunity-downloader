"""Batch download endpoints: a season or series is enumerated server-side.

The browser used to loop one POST per episode, which left the server unable to
say when a season was finished.
"""

import pytest

from app import downloads_notify
from app.auth.permissions import ALL_PERMISSIONS
from tests.conftest import do_setup, make_user, session_for


@pytest.fixture(autouse=True)
def _clean_batches():
    downloads_notify._batches.clear()
    yield
    downloads_notify._batches.clear()


@pytest.fixture
def panel(client, admin_credentials, source, stub_jobs, monkeypatch):
    """Configured panel; the fake source publishes 3 episodes across 1 season."""
    do_setup(client, admin_credentials)
    from app.core import tv

    monkeypatch.setattr(tv, "get_info_tv", lambda *a, **k: 1)
    boss = make_user("boss", "jf-boss-id", int(ALL_PERMISSIONS))
    client.cookies.clear()
    source.csrf = session_for(client, boss.id)
    source.user = boss
    return source


SEASON_BODY = {"tv_id": 77, "slug": "test-series", "tv_name": "Test Series", "season": 1}
SERIES_BODY = {"tv_id": 77, "slug": "test-series", "tv_name": "Test Series"}
ANIME_BODY = {"anime_id": "55", "anime_name": "Test Anime"}


def _post(client, panel, path, body):
    return client.post(path, json=body, headers={"X-CSRF-Token": panel.csrf})


def test_a_season_creates_one_job_per_episode_sharing_a_batch(client, panel, stub_jobs):
    response = _post(client, panel, "/api/download/season", SEASON_BODY)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["count"] == 3
    assert len(body["job_ids"]) == 3
    # stub_jobs binds against the real submit_episode signature, so a kwarg the
    # job manager does not accept fails here rather than in production.
    assert [name for name, _, _ in stub_jobs] == ["submit_episode"] * 3
    batch_ids = {kwargs["batch_id"] for _, _, kwargs in stub_jobs}
    assert batch_ids == {body["batch_id"]}
    assert {kwargs["batch_kind"] for _, _, kwargs in stub_jobs} == {"season"}
    assert {kwargs["batch_label"] for _, _, kwargs in stub_jobs} == {"Test Series — Stagione 1"}


def test_a_series_walks_every_season(client, panel, stub_jobs, monkeypatch):
    from app.core import tv

    monkeypatch.setattr(tv, "get_info_tv", lambda *a, **k: 2)

    response = _post(client, panel, "/api/download/series", SERIES_BODY)

    assert response.status_code == 202
    assert response.json()["count"] == 6  # 2 seasons × 3 episodes
    assert {kwargs["batch_kind"] for _, _, kwargs in stub_jobs} == {"series"}


def test_the_anime_endpoint_enumerates_animeunity(client, panel, stub_jobs):
    response = _post(client, panel, "/api/download/anime-all", ANIME_BODY)

    assert response.status_code == 202
    assert response.json()["count"] == 3
    assert [name for name, _, _ in stub_jobs] == ["submit_anime_episode"] * 3
    assert {kwargs["batch_kind"] for _, _, kwargs in stub_jobs} == {"anime_all"}


def test_a_season_with_no_episodes_is_refused(client, panel, stub_jobs):
    panel.dead = True

    response = _post(client, panel, "/api/download/season", SEASON_BODY)

    assert response.status_code == 409
    assert stub_jobs == []
    # Nothing was registered, so no batch is left waiting for jobs that will
    # never run.
    assert downloads_notify.pending_batches() == 0


def test_a_batch_above_the_cap_is_refused(client, panel, stub_jobs, monkeypatch):
    from app.core import tv

    many = [{"id": 900 + n, "n": str(n), "name": f"Ep {n}"} for n in range(1, 60)]
    monkeypatch.setattr(tv, "get_info_season", lambda *a, **k: list(many))
    monkeypatch.setattr(tv, "get_info_tv", lambda *a, **k: 20)  # 20 × 59 = 1180

    response = _post(client, panel, "/api/download/series", SERIES_BODY)

    assert response.status_code == 400
    assert "Troppi episodi" in response.json()["detail"]
    assert stub_jobs == []


def test_the_client_cannot_choose_the_source_domain(client, panel, stub_jobs):
    """The domain comes from the configuration, never from the body."""
    response = _post(client, panel, "/api/download/season",
                     {**SEASON_BODY, "domain": "evil.test"})

    assert response.status_code == 202
    domains = {args[3] for _, args, _ in stub_jobs}
    assert domains == {"example.test"}


def test_a_scheduled_batch_creates_scheduled_jobs(client, panel, stub_jobs, monkeypatch):
    from app.jobs import job_manager
    from app.schedule import ScheduleStore

    scheduled: list[dict] = []
    monkeypatch.setattr(
        job_manager, "schedule_episode",
        lambda *a, **k: scheduled.append(k) or f"job-{len(scheduled)}",
    )

    response = _post(client, panel, "/api/download/season",
                     {**SEASON_BODY, "scheduled_at": "2999-01-01T00:00:00Z"})

    assert response.status_code == 202
    assert response.json()["status"] == "scheduled"
    assert len(scheduled) == 3
    assert {k["batch_kind"] for k in scheduled} == {"season"}


def test_the_batch_carries_the_acting_user(client, panel, stub_jobs):
    _post(client, panel, "/api/download/season", SEASON_BODY)

    assert {kwargs["user_id"] for _, _, kwargs in stub_jobs} == {panel.user.id}


def test_a_batch_is_registered_before_any_job_is_submitted(client, panel, monkeypatch):
    """A job can fail the instant it is created; its batch must already exist."""
    from app.jobs import job_manager

    seen_at_submit = []

    def spy(*args, **kwargs):
        seen_at_submit.append(downloads_notify.pending_batches())
        return "job-x"

    monkeypatch.setattr(job_manager, "submit_episode", spy)

    _post(client, panel, "/api/download/season", SEASON_BODY)

    assert seen_at_submit == [1, 1, 1]
