"""The second road to a playlist.

The panel had one: the source's iframe, then the vixcloud embed page. When
Cloudflare answers that page with a challenge the scraper cannot clear, every
download of every title stops. vixsrc.to serves the same streams keyed by TMDB
id, so a title whose id we already know has somewhere else to go.

Two behaviours here matter more than the parsing:

The fallback is only ever tried after the primary has failed, so a working
download never changes shape.

Without a tmdb_id the *original* error is re-raised. A user told "missing tmdb
id" when what actually happened was a Cloudflare block has been sent to debug
the wrong thing.
"""

import pytest

from app.core import _shared


PAGE = """
<html><body><script>
  window.canPlayFHD = true;
  window.masterPlaylist = {
    params: {
      'token': 'abc123',
      'expires': '1799999999',
    },
    url: 'https://vixcloud.co/playlist/55555',
  }
</script></body></html>
"""

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/playlist.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080
1080/playlist.m3u8
"""

MEDIA = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="https://vixcloud.co/storage/enc.key",IV=0x00
#EXTINF:6.0,
seg-1.ts
"""

MEDIA_UNENCRYPTED = """#EXTM3U
#EXTINF:6.0,
seg-1.ts
"""

MEDIA_HOSTILE_KEY = """#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="http://evil.example/key.bin",IV=0x00
#EXTINF:6.0,
seg-1.ts
"""


class _Response:
    def __init__(self, text="", status_code=200, content=b"\x11" * 16):
        self.text = text
        self.status_code = status_code
        self.content = content
        self.ok = status_code < 400

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(f"HTTP {self.status_code}")


# ── Parsing ───────────────────────────────────────────────────────────────────

def test_the_playlist_and_its_token_are_read_from_the_page():
    url, params, fhd = _shared.parse_vixsrc_page(PAGE, "https://vixsrc.to/movie/1")
    assert url == "https://vixcloud.co/playlist/55555"
    assert params == {"token": "abc123", "expires": "1799999999"}
    assert fhd is True


def test_a_page_without_a_playlist_is_an_error():
    with pytest.raises(RuntimeError, match="Nessuna playlist"):
        _shared.parse_vixsrc_page("<html></html>", "https://vixsrc.to/movie/1")


def test_a_page_without_a_token_is_an_error():
    html = "<script>window.masterPlaylist = { url: 'https://x/playlist/1' }</script>"
    with pytest.raises(RuntimeError, match="Nessun token"):
        _shared.parse_vixsrc_page(html, "https://vixsrc.to/movie/1")


def test_fhd_is_off_when_the_page_says_so():
    _, _, fhd = _shared.parse_vixsrc_page(
        PAGE.replace("canPlayFHD = true", "canPlayFHD = false"), "x"
    )
    assert fhd is False


# ── URL building ──────────────────────────────────────────────────────────────

PARAMS = {"token": "abc123", "expires": "1799999999"}


def test_a_clean_url_gets_a_question_mark():
    url = _shared.build_vixsrc_playlist_url("https://h/playlist/1", PARAMS, False)
    assert url == "https://h/playlist/1?expires=1799999999&token=abc123"


def test_a_url_that_already_has_a_query_gets_an_ampersand():
    """A second '?' produces a link the CDN rejects."""
    url = _shared.build_vixsrc_playlist_url("https://h/playlist/1?lang=it", PARAMS, False)
    assert url == "https://h/playlist/1?lang=it&expires=1799999999&token=abc123"
    assert url.count("?") == 1


def test_fhd_adds_the_height_flag():
    assert _shared.build_vixsrc_playlist_url("https://h/p/1", PARAMS, True).endswith("&h=1")


# ── The AES key, read rather than assumed ─────────────────────────────────────

@pytest.fixture
def playlists(monkeypatch):
    """Serve a master, its best variant, and the key. Records what was fetched."""
    fetched = []

    def fake_get(url, *args, **kwargs):
        fetched.append(url)
        if url.endswith("enc.key"):
            return _Response(content=b"\x11" * 16)
        if "1080/playlist.m3u8" in url:
            return _Response(MEDIA)
        return _Response(MASTER)

    monkeypatch.setattr(_shared.requests, "get", fake_get)
    from app.core import m3u8

    monkeypatch.setattr(m3u8.requests, "get", fake_get)
    return fetched


def test_the_key_is_taken_from_the_media_playlist(playlists):
    """The master usually carries no EXT-X-KEY; the variant does."""
    key = _shared.fetch_key_from_playlist("https://vixcloud.co/playlist/1", "https://vixsrc.to/")

    assert key == "11" * 16
    assert any("1080/playlist.m3u8" in url for url in playlists)


def test_the_best_variant_is_the_one_followed(playlists):
    _shared.fetch_key_from_playlist("https://vixcloud.co/playlist/1", "https://vixsrc.to/")
    assert not any("360/playlist.m3u8" in url for url in playlists)


def test_an_unencrypted_playlist_has_no_key(monkeypatch):
    def fake_get(url, *args, **kwargs):
        return _Response(MEDIA_UNENCRYPTED if "1080" in url else MASTER)

    monkeypatch.setattr(_shared.requests, "get", fake_get)
    from app.core import m3u8

    monkeypatch.setattr(m3u8.requests, "get", fake_get)

    assert _shared.fetch_key_from_playlist("https://vixcloud.co/playlist/1", "r") is None


def test_a_key_on_someone_elses_host_is_refused(monkeypatch):
    """The playlist is published by the stream page; it does not get to pick hosts."""
    reached = []

    def fake_get(url, *args, **kwargs):
        reached.append(url)
        return _Response(MEDIA_HOSTILE_KEY if "1080" in url else MASTER)

    monkeypatch.setattr(_shared.requests, "get", fake_get)
    from app.core import m3u8

    monkeypatch.setattr(m3u8.requests, "get", fake_get)

    with pytest.raises(RuntimeError, match="host non consentito"):
        _shared.fetch_key_from_playlist("https://vixcloud.co/playlist/1", "r")

    assert not any("evil.example" in url for url in reached)


# ── resolve_stream ────────────────────────────────────────────────────────────

def _source():
    return _shared.StreamSource("https://x/p.m3u8", None, "r", "vixcloud")


def test_a_working_primary_is_never_second_guessed(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("the fallback ran despite a working primary")

    monkeypatch.setattr(_shared, "fetch_vixsrc", boom)

    assert _shared.resolve_stream(_source, tmdb_id=1396).provider == "vixcloud"


def test_the_fallback_runs_when_the_primary_fails(monkeypatch):
    monkeypatch.setattr(
        _shared, "fetch_vixsrc",
        lambda *a, **k: _shared.StreamSource("https://v/p.m3u8", None, "r", "vixsrc"),
    )

    def failing():
        raise RuntimeError("403 Cloudflare")

    assert _shared.resolve_stream(failing, tmdb_id=1396).provider == "vixsrc"


def test_without_a_tmdb_id_the_original_error_survives(monkeypatch):
    """Reporting a missing id sends the user to debug something that is not wrong."""
    def failing():
        raise RuntimeError("403 Cloudflare")

    with pytest.raises(RuntimeError, match="403 Cloudflare"):
        _shared.resolve_stream(failing, tmdb_id=None)


def test_both_failures_are_reported_together(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("vixsrc 404")

    monkeypatch.setattr(_shared, "fetch_vixsrc", boom)

    def failing():
        raise RuntimeError("403 Cloudflare")

    with pytest.raises(_shared.StreamResolutionError) as excinfo:
        _shared.resolve_stream(failing, tmdb_id=1396)

    message = str(excinfo.value)
    assert "403 Cloudflare" in message
    assert "vixsrc 404" in message


def test_an_episode_needs_a_season_and_an_episode(monkeypatch):
    monkeypatch.setattr(_shared, "_get_scraper", lambda: pytest.fail("should not fetch"))

    with pytest.raises(RuntimeError, match="Stagione o episodio"):
        _shared.fetch_vixsrc(1396, "tv")


def test_the_episode_url_carries_season_and_episode(monkeypatch, playlists):
    asked = []

    class _Scraper:
        def get(self, url, *args, **kwargs):
            asked.append(url)
            return _Response(PAGE)

    monkeypatch.setattr(_shared, "_get_scraper", lambda: _Scraper())

    _shared.fetch_vixsrc(1396, "tv", season=2, episode=5)
    assert asked == ["https://vixsrc.to/tv/1396/2/5"]


def test_the_movie_url_has_no_season(monkeypatch, playlists):
    asked = []

    class _Scraper:
        def get(self, url, *args, **kwargs):
            asked.append(url)
            return _Response(PAGE)

    monkeypatch.setattr(_shared, "_get_scraper", lambda: _Scraper())

    source = _shared.fetch_vixsrc(1396, "movie")
    assert asked == ["https://vixsrc.to/movie/1396"]
    assert source.provider == "vixsrc"
    assert "token=abc123" in source.m3u8_url
