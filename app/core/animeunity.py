"""AnimeUnity: search, title details, episodes and downloads.

In November 2026 the site was rebuilt (#50) and every path the panel used went
with it: ``/archivio/get-animes``, ``/info_api/{id}`` and ``/embed-url/{id}`` all
answer 404. The new site is an Inertia app, like StreamingCommunity, so most of
what the panel needs is read out of the ``data-page`` attribute of a page any
visitor is served — no CSRF token, no XHR headers, no session:

- search: ``GET /archive?query=…&page=N``, thirty titles in ``props.animes``;
- title: ``GET /anime/{id}-{slug}``, the record in ``props.anime`` plus the
  source's own ``relatedAnimes``;
- player: ``GET /anime/{id}-{slug}/{video}``, the signed vixcloud URL in
  ``props.embedUrl``;
- episodes: ``GET /api/anime/{id}/episodes?start=N``, plain JSON, 120 a time.

**The data model changed with it.** An Italian dub used to be a title of its
own ("One Piece (ITA)", separate id, separate episodes). Now there is one title
with a ``variants`` entry per language, and every episode carries a ``videos``
list — one stream per language, each with its own id on the player page. So
the language of an anime download is *which video is fetched*, not a track
picked out of a playlist: the streams carry a single muxed soundtrack and
advertise no renditions at all. ``pick_video()`` is that choice, and it obeys
the same rule as every other download — with ``strict`` set a language that is
not on offer is an error, never a substitution.

Old dub ids did not all survive the merge. Some kept their number (the slug
lost its ``-ita``), others answer 301 to the title they were folded into, and
the episodes API answers 404 for those. ``resolve_id()`` follows that redirect
once and remembers it, so a watch or request filed under an old id keeps
working.
"""

import logging
import re
import threading

import requests

from app.core.headers import get_headers

logger = logging.getLogger(__name__)


def host() -> str:
    """AnimeUnity's current host. Read per call: Settings can change it live."""
    from app.config import configured_animeunity_host
    return configured_animeunity_host()


# What /archive answers per page. Not ours to choose — it is the source's page
# size, and what one "Carica altri" is worth.
ARCHIVE_PAGE = 30

# A ceiling on what one call may normalise, for the day the source answers with
# more than a page.
MAX_RESULTS = 60

# The source's own kinds, which are also the panel's wire vocabulary: the new
# site speaks lower case, so there is nothing left to map. Kept as a set so the
# router and the MCP tool can refuse anything else before a request is made.
TYPES = frozenset({"movie", "tv", "ova", "ona", "special", "tv_short"})

# A long series runs to well over a thousand episodes, 120 a page. The bound is
# for a source that keeps answering with something: it is never reached by a
# real title (One Piece is ten pages).
_MAX_EPISODE_PAGES = 60

_STATUS_LABELS = {
    "ongoing": "In corso", "completed": "Terminato",
    "upcoming": "In arrivo", "dropped": "Interrotto",
}
_SEASON_LABELS = {
    "winter": "Inverno", "spring": "Primavera", "summer": "Estate", "fall": "Autunno",
}

_scraper = None

# Old id → the id the source answers to now. See resolve_id().
_moved: dict[int, int] = {}
_moved_lock = threading.Lock()


def _get_scraper():
    global _scraper
    if _scraper is None:
        try:
            import cloudscraper
            _scraper = cloudscraper.create_scraper(
                browser={"browser": "firefox", "platform": "darwin", "desktop": True}
            )
        except ImportError:
            logger.warning("cloudscraper not installed, falling back to requests.Session")
            _scraper = requests.Session()
    return _scraper


def _get(path: str, *, params: dict | None = None, allow_redirects: bool = True,
         headers: dict | None = None, timeout: int = 20):
    return _get_scraper().get(
        f"https://{host()}{path}", params=params, allow_redirects=allow_redirects,
        headers={"user-agent": get_headers(), **(headers or {})}, timeout=timeout,
    )


def _props(path: str, *, params: dict | None = None) -> dict:
    """The Inertia ``props`` of one page of the site."""
    from app.core.page import _data_page

    r = _get(path, params=params)
    if r.status_code == 404:
        raise LookupError(f"AnimeUnity: {path} non trovato (HTTP 404)")
    if not r.ok:
        raise RuntimeError(f"AnimeUnity: HTTP {r.status_code} su {path}")
    try:
        return _data_page(r.text).get("props") or {}
    except LookupError:
        raise RuntimeError(f"AnimeUnity: {path} non contiene i dati attesi")


def fetch_home_props() -> dict:
    """The front page's props, for the start page's shelves."""
    return _props("/")


# ── Ids ───────────────────────────────────────────────────────────────────────

_ID = re.compile(r"^\s*(\d+)")
_LOCATION_ID = re.compile(r"/anime/(\d+)(?:-|/|$)")


def parse_id(anime_id) -> int:
    """The numeric id inside a panel id (``"12-one-piece"`` → 12)."""
    m = _ID.match(str(anime_id))
    if not m:
        raise ValueError(f"id AnimeUnity non valido: {anime_id!r}")
    return int(m.group(1))


def _title_path(anime_id) -> str:
    """``/anime/{id}-{slug}``. The site answers a bare id with 405, and corrects
    a wrong slug with a redirect, so any slug will do when there is none."""
    raw = str(anime_id).strip()
    return f"/anime/{raw}" if "-" in raw else f"/anime/{parse_id(raw)}-x"


def resolve_id(anime_id) -> int:
    """The id the source answers to now for ``anime_id``.

    Usually the number it already carries. A dub that was folded into its
    original's record answers its old title page with a 301 to the new one,
    and that is followed once and remembered for the life of the process.
    """
    num = parse_id(anime_id)
    with _moved_lock:
        if num in _moved:
            return _moved[num]
    r = _get(_title_path(anime_id), allow_redirects=False)
    target = num
    if r.status_code in (301, 302, 307, 308):
        m = _LOCATION_ID.search(r.headers.get("location", ""))
        if m:
            target = int(m.group(1))
    elif r.status_code == 404:
        raise LookupError(f"anime {anime_id} non più presente su AnimeUnity")
    if target != num:
        logger.info("AnimeUnity id %s now answers as %s", num, target)
    with _moved_lock:
        _moved[num] = target
    return target


# ── Titles ────────────────────────────────────────────────────────────────────

def _names(value) -> list[str]:
    """Names out of a list of ``{id, name}`` rows, or of plain strings."""
    names = []
    for g in (value if isinstance(value, list) else []):
        name = g.get("name") if isinstance(g, dict) else g
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def title_languages(t: dict) -> list[str]:
    """The languages a title is published in, from its ``variants``."""
    langs = []
    for v in t.get("variants") or []:
        lang = str((v or {}).get("language") or "").strip().lower() if isinstance(v, dict) else ""
        if lang and lang not in langs:
            langs.append(lang)
    return langs


def normalize_title(t: dict) -> dict | None:
    """One raw record into the shape the panel renders, or None if unusable."""
    if not isinstance(t, dict):
        return None
    name = str(t.get("title") or t.get("title_it") or t.get("title_ja") or "").strip()
    id_num = t.get("id")
    if not name or id_num is None:
        return None
    slug = t.get("slug") or ""
    images = t.get("images") if isinstance(t.get("images"), dict) else {}
    languages = title_languages(t)
    counts = [int(v.get("episodes_count") or 0) for v in t.get("variants") or []
              if isinstance(v, dict) and str(v.get("episodes_count") or "0").isdigit()]
    year = str(t.get("year") or "")
    original = str(t.get("title_ja") or "").strip()
    return {
        "id": f"{id_num}-{slug}" if slug else str(id_num),
        "name": name,
        "type": "anime",
        # The source's own classification — tv, movie, ova, ona, special,
        # tv_short. Kept beside `type` rather than in it: `type` is "anime" for
        # every record and the whole anime flow keys off that, from the detail
        # page to the episodes endpoint to the job kind.
        "media_type": str(t.get("type") or "").lower(),
        "slug": slug,
        # Absolute URLs, unlike StreamingCommunity's bare filenames: the
        # frontend branches on that and sends only the latter through /api/image.
        "poster": images.get("poster") or images.get("cover") or "",
        "backdrop": images.get("banner") or images.get("cover") or "",
        "logo": images.get("logo") or "",
        "episodes_count": max(counts) if counts else int(t.get("total_episodes") or 0),
        "plot": t.get("plot") or "",
        "genres": _names(t.get("genres")),
        # One title per show now, with a variant per language. `dubbed` keeps
        # its meaning for the search filter and the card: an Italian dub exists.
        "languages": languages,
        "dubbed": "ita" in languages,
        "score": t.get("score"),
        "release_date": year,
        "studio": ", ".join(_names(t.get("studios"))),
        "status": _STATUS_LABELS.get(str(t.get("default_status") or ""), t.get("default_status") or ""),
        "season": _SEASON_LABELS.get(str(t.get("season") or ""), t.get("season") or ""),
        "original_name": original if original and original != name else "",
    }


def _normalize_titles(titles) -> list[dict]:
    results, seen = [], set()
    for t in titles if isinstance(titles, list) else []:
        item = normalize_title(t)
        if item is None or item["id"] in seen:
            continue
        seen.add(item["id"])
        results.append(item)
        if len(results) >= MAX_RESULTS:
            break
    return results


def search(query: str, *, page: int = 1, media_type: str | None = None,
           dubbed: bool = False) -> list[dict]:
    """Search AnimeUnity's archive.

    Both filters go to the source rather than to what comes back: filtering
    here could only ever narrow one page, and would answer "no Italian dub" for
    a show whose dub sat on page two. So an empty page really is the end.
    """
    params = {"query": query, "page": max(page, 1)}
    if media_type:
        kind = media_type.lower()
        if kind not in TYPES:
            raise ValueError(f"tipo AnimeUnity non valido: {media_type!r}")
        params["type"] = kind
    if dubbed:
        params["language"] = "ita"
    props = _props("/archive", params=params)
    animes = props.get("animes") or {}
    # No rows is an answer, not a failure — the same one StreamingCommunity gives.
    return _normalize_titles(animes.get("data") if isinstance(animes, dict) else animes)


def get_title(anime_id) -> dict:
    """One title's record and the source's related titles (sequels, films, OVAs)."""
    props = _props(_title_path(anime_id))
    item = normalize_title(props.get("anime") or {})
    if item is None:
        raise RuntimeError(f"AnimeUnity: scheda di {anime_id} illeggibile")
    item["related"] = _normalize_titles(props.get("relatedAnimes"))
    return item


# ── Episodes ──────────────────────────────────────────────────────────────────

def _episode(raw: dict) -> dict | None:
    """One episode with the videos it can be downloaded as, one per language."""
    if not isinstance(raw, dict) or raw.get("id") is None:
        return None
    # `is_hidden` is not "unavailable". The site's own player never reads it
    # and plays these videos, and they resolve to full 1080p playlists: it is
    # set on whole back catalogues — Vinland Saga's Italian dub on episodes
    # 1-23 of 24, Bleach's on 118 of 120, a Japanese original here and there —
    # the shape of the dub records folded into their originals. Skipping them
    # made a show the site offers dubbed look subtitled-only. It only breaks a
    # tie: a visible video wins over a hidden one in the same language.
    by_lang: dict[str, tuple[bool, dict]] = {}
    for v in raw.get("videos") or []:
        if not isinstance(v, dict) or v.get("id") is None:
            continue
        lang = str(v.get("language") or "").strip().lower()
        hidden = bool(v.get("is_hidden"))
        if lang and (lang not in by_lang or (by_lang[lang][0] and not hidden)):
            by_lang[lang] = (hidden, {"id": v["id"], "language": lang, "runtime": v.get("runtime")})
    videos = [video for _, video in by_lang.values()]
    return {
        "id": raw["id"],
        "number": str(raw.get("number") if raw.get("number") is not None else ""),
        "seq": raw.get("seq_number_start"),
        "is_filler": bool(raw.get("is_filler")),
        "videos": videos,
        "languages": [v["language"] for v in videos],
    }


def _episodes_page(num: int, start: int | None) -> dict:
    params = {"start": start} if start is not None else None
    r = _get(f"/api/anime/{num}/episodes", params=params,
             headers={"accept": "application/json"})
    if r.status_code == 404:
        raise LookupError(f"anime {num} non trovato")
    r.raise_for_status()
    return r.json()


def _fetch_episodes(anime_id, start: int | None = None) -> tuple[int, dict]:
    """The canonical id and one page of episodes, following a merged id."""
    num = parse_id(anime_id)
    with _moved_lock:
        num = _moved.get(num, num)
    try:
        return num, _episodes_page(num, start)
    except LookupError:
        moved = resolve_id(anime_id)
        if moved == num:
            raise
        return moved, _episodes_page(moved, start)


def get_episodes(anime_id) -> list[dict]:
    """Every episode of an anime, in the source's order.

    ``start`` is a sequence number, not a row offset, and a page covers
    ``tab_size`` of them; the next page starts after the last sequence number
    seen, which stays right if the numbering has holes.
    """
    num, first = _fetch_episodes(anime_id)
    total = int(first.get("total") or 0)
    tab = int(first.get("tab_size") or 120)
    episodes, seen = [], set()

    def take(batch) -> int:
        added = 0
        for raw in batch or []:
            ep = _episode(raw)
            if ep and ep["id"] not in seen:
                seen.add(ep["id"])
                episodes.append(ep)
                added += 1
        return added

    take(first.get("episodes"))
    start = int(first.get("start") or first.get("offset") or 1)
    for _ in range(_MAX_EPISODE_PAGES):
        if len(episodes) >= total:
            break
        last = max((int(e["seq"]) for e in episodes if isinstance(e.get("seq"), int)), default=None)
        start = (last + 1) if last is not None and last >= start else start + tab
        if not take(_episodes_page(num, start).get("episodes")):
            break
    return episodes


def find_episode(anime_id, number, hint=None) -> dict:
    """One episode by number, reading the page it sits on rather than the lot.

    ``hint`` is the episode's sequence number when the caller has it; a number
    that is a plain integer is the next best guess. Either way the full list is
    the fallback, so a wrong guess costs a request, never a wrong episode.
    """
    wanted = str(number)
    guess = hint
    if guess is None:
        try:
            guess = int(float(wanted))
        except ValueError:
            guess = None
    if guess is not None:
        _, page = _fetch_episodes(anime_id, max(int(guess), 1))
        for raw in page.get("episodes") or []:
            ep = _episode(raw)
            if ep and ep["number"] == wanted:
                return ep
    for ep in get_episodes(anime_id):
        if ep["number"] == wanted:
            return ep
    raise LookupError(f"episodio {wanted} non più presente su AnimeUnity")


def get_episode_languages(episode: dict) -> dict:
    """The languages an episode is published in.

    Each one is a separate stream with its soundtrack muxed in, so they are
    reported as audio. There are no subtitle tracks to pick: a version
    subtitled by the source carries them in the picture.
    """
    return {"audio": list((episode or {}).get("languages") or
                          [v["language"] for v in (episode or {}).get("videos") or []]),
            "subtitles": []}


def offers(episode: dict, audio_languages) -> bool:
    """Whether ``episode`` has a video in any of ``audio_languages``."""
    wanted = {str(l).lower() for l in audio_languages or []}
    return any(v["language"] in wanted for v in episode.get("videos") or [])


def pick_video(episode: dict, audio_languages, strict: bool = False) -> dict:
    """The video to download for ``audio_languages``.

    One file holds one stream, so only one language can win: Italian when it
    was asked for, otherwise the first asked for that is on offer. With
    ``strict`` a language that is not on offer raises — the request flow must
    not hand a user who asked for the dub the original, or the reverse.
    Without it, the Italian video (or the only one) stands in, as a direct
    download always has when the source could not do what was asked.
    """
    from app.core._shared import MissingAudioTrackError

    videos = episode.get("videos") or []
    by_lang = {v["language"]: v for v in videos}
    wanted = [str(l).lower() for l in audio_languages or []]
    for lang in sorted(dict.fromkeys(wanted), key=lambda l: l != "ita"):
        if lang in by_lang:
            return by_lang[lang]
    if strict or not videos:
        raise MissingAudioTrackError(wanted[0] if wanted else "?", list(by_lang))
    fallback = by_lang.get("ita") or videos[0]
    logger.warning("Episode %s: %s not offered, downloading %s",
                   episode.get("number"), wanted or "-", fallback["language"])
    return fallback


# ── Download ──────────────────────────────────────────────────────────────────

def _embed_url(anime_id, video_id) -> str:
    """The signed vixcloud URL the player page hands its iframe."""
    props = _props(f"{_title_path(anime_id)}/{video_id}")
    served = (props.get("video") or {}).get("id")
    # The page answers a video that is not this title's with 404, but a
    # payload naming another video would download the wrong episode in silence.
    if str(served) != str(video_id):
        raise RuntimeError(f"AnimeUnity ha servito il video {served} invece di {video_id}")
    embed_url = str(props.get("embedUrl") or "").strip()
    if not embed_url.startswith("http"):
        raise RuntimeError(f"embedUrl inatteso: {embed_url[:100]!r}")
    return embed_url


def _get_embed_content(anime_id, video_id) -> tuple[str, str]:
    """The vixcloud embed page's script and URL — same shape as film._get_iframe()."""
    from bs4 import BeautifulSoup

    from app.core._shared import apply_video_host

    embed_url = apply_video_host(_embed_url(anime_id, video_id))
    logger.info("Anime %s video %s embed URL: %s", anime_id, video_id, embed_url[:80])

    # vixcloud serves the /embed/ page behind Cloudflare, so plain requests.get
    # gets a challenge page; the cloudscraper session clears it. (Playlist,
    # enc.key and the segments are not gated.)
    req_embed = _get_scraper().get(
        embed_url, headers={"Referer": f"https://{host()}/"}, timeout=15,
    )
    req_embed.raise_for_status()

    body = BeautifulSoup(req_embed.text, "lxml").find("body")
    if not body:
        raise RuntimeError("Empty embed page body")
    script = body.find("script")
    if not script:
        raise RuntimeError("No script tag found in vixcloud embed page")
    return script.text, embed_url


def download_anime_episode(
    anime_id: str,
    episode: dict,
    anime_name: str,
    anime_type: str = "tv",
    output_dir: str = "videos",
    temp_dir: str = None,
    progress_factory=None,
    cancel_event=None,
    year: str = None,
    audio_languages: list[str] = None,
    subtitle_languages: list[str] = None,
    strict_audio: bool = False,
) -> str:
    """
    Full download pipeline for a single anime episode.
    - Movies (anime_type="movie" or "film", any case): videos/AnimeName (YYYY)/AnimeName.mkv
    - Anything else (TV, ONA, OVA, Special, …): videos/AnimeName/Season 01/AnimeName S01E01.mkv

    The extension is whatever app.core.container is configured to write.
    """
    audio_languages = audio_languages or ["ita"]
    subtitle_languages = subtitle_languages or []

    from app.core.film import _collect_audio_tracks, _collect_subtitle_tracks
    from app.core._shared import _parse_content, _get_m3u8_url, _get_m3u8_key
    from app.core.m3u8 import download_m3u8
    from app.core.paths import anime_path

    episode_number = str(episode.get("number", "0"))
    # A record that came through the browser, a schedule written before #50 or
    # an API caller carries no videos: the episode is read again from the
    # source, which also means the choice below is made on what is live now.
    if not episode.get("videos"):
        episode = find_episode(anime_id, episode_number, hint=episode.get("seq"))
    video = pick_video(episode, audio_languages, strict=strict_audio)
    # The stream holds one soundtrack, in the video's language. Asking the
    # playlist for any other would only find nothing.
    stream_audio = [video["language"]]

    embed_content, embed_url = _get_embed_content(anime_id, video["id"])
    json_win_video, json_win_param = _parse_content(embed_content, embed_url)

    logger.info(
        "Anime episode %s (%s) — video_id=%s token=%.8s...",
        episode_number, video["language"], json_win_video.get("id"),
        json_win_param.get("token", ""),
    )

    m3u8_url = _get_m3u8_url(json_win_video, json_win_param)
    m3u8_key = _get_m3u8_key(json_win_video, json_win_param, embed_url)

    audio_track_urls = _collect_audio_tracks(m3u8_url, embed_url, stream_audio, strict=strict_audio)
    subtitle_track_urls = _collect_subtitle_tracks(m3u8_url, embed_url, subtitle_languages)

    # A folder a person associated with this anime wins — which is how the
    # parts of one series, separate titles at the source, land in one folder
    # at the season and episode their offsets say (app/library.py).
    from app.core.paths import is_anime_series
    from app.library import destination

    dest_path = destination("anime", anime_id, root=output_dir, episode=episode_number,
                            year=year, single_file=not is_anime_series(anime_type))
    if not dest_path and is_anime_series(anime_type):
        # The series' folder already on disk, whatever year it carries (#24).
        from app.requests.resolver import ANIME, series_episode_path

        dest_path = series_episode_path(ANIME, anime_id, output_dir, anime_name, 1,
                                        episode_number, year, anime_type=anime_type)
    dest_path = dest_path or anime_path(output_dir, anime_name, episode_number, anime_type, year)

    final_path = download_m3u8(
        m3u8_index=m3u8_url,
        key=m3u8_key,
        output_filename=dest_path,
        temp_dir=temp_dir,
        progress_factory=progress_factory,
        referer=embed_url,
        cancel_event=cancel_event,
        audio_languages=stream_audio,
        subtitle_languages=subtitle_languages,
        audio_track_urls=audio_track_urls,
        subtitle_track_urls=subtitle_track_urls,
    )

    # download_m3u8 returns the real output path: the destination is built in
    # whatever container is configured, and the remux keeps it.
    return final_path or dest_path
