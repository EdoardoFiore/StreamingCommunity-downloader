"""AnimeUnity after its rebuild (#50): what is asked for, and what it becomes.

The site moved every path the panel used and changed its model with them: an
Italian dub is no longer a title of its own but a second video on every
episode. The pins here are the ones that model change makes easy to get wrong —
which video a download fetches, which episodes a watch counts as published,
and what an existing watch meant before the change.
"""

import html
import json
from urllib.parse import urlsplit

import pytest
import requests

from app import db
from app.core import animeunity
from app.core._shared import MissingAudioTrackError


# ── A fake site ───────────────────────────────────────────────────────────────

class _Response:
    def __init__(self, status_code=200, text="", payload=None, location=None):
        self.status_code = status_code
        self.text = text
        self._payload = payload
        self.headers = {"location": location} if location else {}

    @property
    def ok(self):
        return self.status_code < 400

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def _page(props, component="Page"):
    return _Response(text='<html><body><div id="app" data-page="'
                     + html.escape(json.dumps({"component": component, "props": props}),
                                   quote=True)
                     + '"></div></body></html>')


class _Site:
    """Routes keyed on path; a callable route gets the query params."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def get(self, url, params=None, allow_redirects=True, **kwargs):
        path = urlsplit(url).path
        self.calls.append((path, dict(params or {}), allow_redirects))
        route = self.routes.get(path)
        if route is None:
            return _Response(404, text="not found")
        return route(params or {}) if callable(route) else route


@pytest.fixture
def site(monkeypatch):
    fake = _Site()
    monkeypatch.setattr(animeunity, "_get_scraper", lambda: fake)
    monkeypatch.setattr(animeunity, "host", lambda: "au.test")
    monkeypatch.setattr(animeunity, "_moved", {})
    return fake


def _record(n, kind="tv", languages=("ita", "jpn"), **extra):
    return {
        "id": n, "slug": f"anime-{n}", "title": f"Anime {n}", "title_ja": f"Anime {n} JA",
        "title_it": None, "type": kind, "year": 2020, "season": "fall",
        "default_status": "completed", "score": "8.10", "plot": "Una trama.",
        "images": {"poster": f"https://img.au.test/{n}.jpg", "banner": f"https://img.au.test/{n}b.jpg",
                   "cover": None, "logo": None},
        "genres": [{"id": 1, "name": "Azione"}], "studios": [{"id": 3, "name": "Toei"}],
        "variants": [{"language": lang, "episodes_count": 12} for lang in languages],
        **extra,
    }


def _archive(*records):
    return _page({"animes": {"current_page": 1, "data": list(records), "per_page": 30}},
                 component="Archive")


def _raw_episode(seq, languages=("ita", "jpn"), number=None, hidden=()):
    return {
        "id": 1000 + seq, "number": str(number if number is not None else seq),
        "seq_number_start": seq, "seq_number_end": seq, "is_filler": False,
        "videos": [{"id": 50000 + seq * 10 + i, "language": lang, "runtime": 1400,
                    "is_hidden": lang in hidden}
                   for i, lang in enumerate(languages)],
    }


def _episodes_api(total, tab=3, languages_for=lambda seq: ("ita", "jpn")):
    """/api/anime/{id}/episodes over ``total`` episodes, ``tab`` a page."""
    def route(params):
        start = int(params.get("start", 1))
        eps = [_raw_episode(s, languages_for(s))
               for s in range(start, min(start + tab, total + 1))]
        return _Response(payload={"episodes": eps, "start": start, "tab_size": tab,
                                  "total": total, "offset": 1})
    return route


# ── Search ────────────────────────────────────────────────────────────────────

def test_the_archive_page_is_read_with_the_query_and_page(site):
    site.routes["/archive"] = _archive(_record(1))

    animeunity.search("one piece", page=3)

    assert site.calls == [("/archive", {"query": "one piece", "page": 3}, True)]


def test_the_filters_go_to_the_source(site):
    """Filtering what came back could only narrow one page."""
    site.routes["/archive"] = _archive()

    animeunity.search("x", media_type="tv_short", dubbed=True)

    assert site.calls[0][1] == {"query": "x", "page": 1, "type": "tv_short", "language": "ita"}


def test_an_unknown_kind_is_refused_before_any_request(site):
    with pytest.raises(ValueError):
        animeunity.search("x", media_type="Movie; drop")
    assert site.calls == []


def test_a_record_becomes_a_panel_result(site):
    site.routes["/archive"] = _archive(_record(12, kind="movie"))

    item = animeunity.search("x")[0]

    assert item["id"] == "12-anime-12"
    # type stays "anime" — the whole anime flow keys off it — and the source's
    # own word rides in media_type.
    assert item["type"] == "anime" and item["media_type"] == "movie"
    assert item["poster"] == "https://img.au.test/12.jpg"
    assert item["backdrop"] == "https://img.au.test/12b.jpg"
    assert item["languages"] == ["ita", "jpn"] and item["dubbed"] is True
    assert item["release_date"] == "2020"
    assert item["genres"] == ["Azione"] and item["studio"] == "Toei"
    assert item["status"] == "Terminato" and item["season"] == "Autunno"
    assert item["original_name"] == "Anime 12 JA"
    assert item["episodes_count"] == 12


def test_a_title_without_a_dub_is_not_dubbed(site):
    site.routes["/archive"] = _archive(_record(1, languages=("jpn",)))

    assert animeunity.search("x")[0]["dubbed"] is False


def test_no_results_is_an_empty_list_and_duplicates_are_one_card(site):
    site.routes["/archive"] = _archive()
    assert animeunity.search("nothing") == []

    site.routes["/archive"] = _archive(_record(1), _record(1), {"id": None})
    assert len(animeunity.search("x")) == 1


def test_the_old_site_is_reported_not_parsed_as_empty(site):
    """No payload is the site having changed again, not a search with no hits."""
    site.routes["/archive"] = _Response(text="<html><body>old</body></html>")

    with pytest.raises(RuntimeError):
        animeunity.search("x")


# ── Episodes ──────────────────────────────────────────────────────────────────

def test_every_page_of_episodes_is_read(site):
    site.routes["/api/anime/12/episodes"] = _episodes_api(total=7, tab=3)

    episodes = animeunity.get_episodes("12-one-piece")

    assert [e["number"] for e in episodes] == [str(n) for n in range(1, 8)]
    starts = [params.get("start") for path, params, _ in site.calls]
    assert starts == [None, 4, 7], "the next page starts after the last sequence number"


def test_an_episode_carries_one_video_per_language(site):
    site.routes["/api/anime/12/episodes"] = lambda p: _Response(payload={
        "episodes": [_raw_episode(1, ("ita", "jpn"))],
        "start": 1, "tab_size": 120, "total": 1})

    episode = animeunity.get_episodes("12")[0]

    assert [v["language"] for v in episode["videos"]] == ["ita", "jpn"]
    assert animeunity.get_episode_languages(episode) == {"audio": ["ita", "jpn"], "subtitles": []}


def test_a_hidden_video_is_still_on_offer(site):
    """Measured on Vinland Saga: the Italian dub of episodes 1-23 is
    ``is_hidden``, the site plays it anyway, and the stream is complete."""
    site.routes["/api/anime/12/episodes"] = lambda p: _Response(payload={
        "episodes": [_raw_episode(1, ("ita", "jpn"), hidden=("ita",))],
        "start": 1, "tab_size": 120, "total": 1})

    episode = animeunity.get_episodes("12")[0]

    assert episode["languages"] == ["ita", "jpn"]
    assert animeunity.offers(episode, ["ita"])
    assert animeunity.pick_video(episode, ["ita"], strict=True)["id"] == 50010


def test_a_visible_video_wins_over_a_hidden_one_in_the_same_language(site):
    raw = _raw_episode(1, ("ita", "jpn", "ita"))
    raw["videos"][0]["is_hidden"] = True
    site.routes["/api/anime/12/episodes"] = lambda p: _Response(payload={
        "episodes": [raw], "start": 1, "tab_size": 120, "total": 1})

    episode = animeunity.get_episodes("12")[0]

    assert episode["languages"] == ["ita", "jpn"], "the source's order is kept"
    assert [v["id"] for v in episode["videos"]] == [50012, 50011]


def test_a_short_page_stops_the_walk(site):
    """A source answering less than it promised must not be asked forever."""
    def route(params):
        start = int(params.get("start", 1))
        eps = [_raw_episode(1)] if start == 1 else []
        return _Response(payload={"episodes": eps, "start": start, "tab_size": 120, "total": 50})
    site.routes["/api/anime/12/episodes"] = route

    assert len(animeunity.get_episodes("12")) == 1
    assert len(site.calls) == 2


def test_a_dub_folded_into_its_original_is_followed(site):
    """Old dub ids answer 404 on the episodes API and 301 on their title page."""
    site.routes["/anime/1104-sword-gai-ita"] = _Response(
        301, location="https://au.test/anime/6351-sword-gai")
    site.routes["/api/anime/6351/episodes"] = _episodes_api(total=2, tab=120)

    episodes = animeunity.get_episodes("1104-sword-gai-ita")

    assert [e["number"] for e in episodes] == ["1", "2"]
    assert animeunity.resolve_id("1104-sword-gai-ita") == 6351

    # Remembered: the next call goes straight to the new id.
    site.calls.clear()
    animeunity.get_episodes("1104-sword-gai-ita")
    assert all(path == "/api/anime/6351/episodes" for path, _, _ in site.calls)


def test_a_title_that_is_gone_is_a_lookup_error(site):
    with pytest.raises(LookupError):
        animeunity.get_episodes("99-nothing")


def test_one_episode_is_found_on_its_own_page(site):
    site.routes["/api/anime/12/episodes"] = _episodes_api(total=1000, tab=120)

    episode = animeunity.find_episode("12-one-piece", "500")

    assert episode["number"] == "500"
    assert len(site.calls) == 1, "one page, not the thousand episodes"


# ── Which video ───────────────────────────────────────────────────────────────

def _episode(*languages):
    return animeunity._episode(_raw_episode(1, languages))


def test_italian_wins_when_it_is_asked_for():
    assert animeunity.pick_video(_episode("ita", "jpn"), ["jpn", "ita"])["language"] == "ita"


def test_the_original_is_downloaded_when_it_is_what_was_asked():
    assert animeunity.pick_video(_episode("ita", "jpn"), ["jpn"])["language"] == "jpn"


def test_strict_never_substitutes_a_version():
    """A request for the dub on an episode the dub has not reached parks the
    request for a person; it must not come down in Japanese."""
    with pytest.raises(MissingAudioTrackError) as exc:
        animeunity.pick_video(_episode("jpn"), ["ita"], strict=True)
    assert exc.value.available == ["jpn"]


def test_a_direct_download_falls_back_to_what_there_is():
    assert animeunity.pick_video(_episode("jpn"), ["ita"])["language"] == "jpn"


def test_the_player_page_must_serve_the_video_asked_for(site):
    site.routes["/anime/12-one-piece/111"] = _page(
        {"video": {"id": 222}, "embedUrl": "https://vixcloud.test/embed/1?token=t"})

    with pytest.raises(RuntimeError, match="222"):
        animeunity._embed_url("12-one-piece", 111)


def test_the_player_page_gives_the_signed_embed_url(site):
    site.routes["/anime/12-x/111"] = _page(
        {"video": {"id": 111}, "embedUrl": "https://vixcloud.test/embed/1?token=t"})

    assert animeunity._embed_url("12", 111) == "https://vixcloud.test/embed/1?token=t"


def test_a_download_reads_the_episode_again_and_fetches_the_chosen_video(site, monkeypatch):
    """The browser posts {id, number}: the videos are read from the source."""
    site.routes["/api/anime/12/episodes"] = _episodes_api(
        total=5, tab=120, languages_for=lambda s: ("ita", "jpn"))
    fetched = []

    def stop(anime_id, video_id):
        fetched.append((anime_id, video_id))
        raise RuntimeError("stop here")

    monkeypatch.setattr(animeunity, "_get_embed_content", stop)

    with pytest.raises(RuntimeError, match="stop here"):
        animeunity.download_anime_episode("12-one-piece", {"id": 1003, "number": "3"},
                                          "One Piece", audio_languages=["jpn"])

    assert fetched == [("12-one-piece", 50000 + 3 * 10 + 1)]


# ── Batches and watches ───────────────────────────────────────────────────────

from app.auth.permissions import ALL_PERMISSIONS, Permission  # noqa: E402
from app.requests import models as request_models  # noqa: E402
from app.watches import models as watch_models, poller  # noqa: E402
from tests.conftest import do_setup, make_user, session_for  # noqa: E402

ANIME_WATCH = {
    "source": "animeunity", "media_type": "anime", "external_id": "55",
    "title": "Test Anime", "anime_type": "tv",
    "audio_languages": ["ita"], "subtitle_languages": [],
}


@pytest.fixture
def panel(client, admin_credentials, source, stub_jobs):
    do_setup(client, admin_credentials)
    return source


def _login(client, username, permissions):
    user = make_user(username, f"jf-{username}-id", int(permissions))
    client.cookies.clear()
    return session_for(client, user.id)


def test_a_batch_queues_only_the_episodes_out_in_the_chosen_version(client, panel, stub_jobs):
    panel.add_anime_episode(4, languages=("jpn",))
    csrf = _login(client, "boss", ALL_PERMISSIONS)

    response = client.post("/api/download/anime-all", headers={"X-CSRF-Token": csrf}, json={
        "anime_id": "55", "anime_name": "Test Anime", "audio_languages": ["ita"]})

    assert response.status_code == 202, response.text
    assert [args[1]["number"] for _, args, _ in stub_jobs] == ["1", "2", "3"]


def test_a_batch_in_a_version_nobody_published_says_which_there_are(client, panel):
    panel.anime_episodes[:] = [panel.anime_episode(n, ("jpn",)) for n in (1, 2)]
    csrf = _login(client, "boss", ALL_PERMISSIONS)

    response = client.post("/api/download/anime-all", headers={"X-CSRF-Token": csrf}, json={
        "anime_id": "55", "anime_name": "Test Anime", "audio_languages": ["ita"]})

    assert response.status_code == 409
    assert "jpn" in response.json()["detail"]


def test_a_watch_on_the_dub_waits_for_the_dub(client, panel):
    """The original's new episode is not new to someone following the dub;
    the dub of it, weeks later, is."""
    csrf = _login(client, "ann", Permission.REQUEST | Permission.DOWNLOAD)
    watch_id = client.post("/api/watches", json=ANIME_WATCH,
                           headers={"X-CSRF-Token": csrf}).json()["id"]

    panel.add_anime_episode(4, languages=("jpn",))
    poller.run_poll_cycle()
    assert request_models.list_all() == []

    panel.anime_episodes[-1] = panel.anime_episode(4, ("ita", "jpn"))
    poller.run_poll_cycle()
    assert [r.episode_number for r in request_models.list_all()] == ["4"]
    assert "E4" in watch_models.seen_keys(watch_id)


def test_changing_an_anime_watch_language_does_not_queue_the_back_catalogue(client, panel):
    """Adding the original to a dub watch makes the episodes the dub has not
    reached "published" — they must count as already handled."""
    for n in (4, 5):
        panel.add_anime_episode(n, languages=("jpn",))
    csrf = _login(client, "ann", Permission.REQUEST | Permission.DOWNLOAD)
    watch_id = client.post("/api/watches", json=ANIME_WATCH,
                           headers={"X-CSRF-Token": csrf}).json()["id"]
    assert watch_models.seen_keys(watch_id) == {"E1", "E2", "E3"}

    response = client.put(f"/api/watches/{watch_id}/languages", headers={"X-CSRF-Token": csrf},
                          json={"audio_languages": ["ita", "jpn"], "subtitle_languages": []})
    assert response.status_code == 200, response.text

    poller.run_poll_cycle()
    assert request_models.list_all() == []
    assert watch_models.seen_keys(watch_id) == {"E1", "E2", "E3", "E4", "E5"}


# ── Migration v11 ─────────────────────────────────────────────────────────────

@pytest.fixture
def v10_db(tmp_path):
    """A database stopped at v10, the last schema before #50."""
    db.configure(tmp_path / "legacy.db")
    full = db.MIGRATIONS
    db.MIGRATIONS = full[:10]
    try:
        db.run_migrations()
    finally:
        db.MIGRATIONS = full
    yield
    db.close_all()


def _legacy_watch(external_id, slug=None, media_type="anime"):
    db.execute(
        "INSERT INTO jf_series_watch(source, media_type, external_id, slug, title, "
        "audio_languages, subtitle_languages, auto_approve, enabled, created_at, updated_at) "
        "VALUES('animeunity', ?, ?, ?, 't', '[\"ita\"]', '[]', 0, 1, 'now', 'now')",
        (media_type, external_id, slug),
    )
    return db.query_one("SELECT last_insert_rowid() AS id")["id"]


def _audio(watch_id):
    return json.loads(db.query_one("SELECT audio_languages FROM jf_series_watch WHERE id = ?",
                                   (watch_id,))["audio_languages"])


def test_an_existing_watch_keeps_following_the_version_it_followed(v10_db):
    """Before #50 the title page always sent ["ita"]; the id decided the version."""
    original = _legacy_watch("12-one-piece")
    dub = _legacy_watch("1160-slam-dunk-ita")
    tv = _legacy_watch("77", media_type="tv")

    db.run_migrations()

    assert _audio(original) == ["jpn"]
    assert _audio(dub) == ["ita"]
    assert _audio(tv) == ["ita"], "a StreamingCommunity watch is not touched"


def test_a_fresh_database_has_nothing_to_carry_over(tmp_path):
    db.configure(tmp_path / "fresh.db")
    try:
        db.run_migrations()
        assert db.query_one("PRAGMA user_version")[0] == len(db.MIGRATIONS)
    finally:
        db.close_all()
