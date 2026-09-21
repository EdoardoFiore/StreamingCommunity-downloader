# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A self-hosted FastAPI web panel that searches and downloads films, TV series and anime from
StreamingCommunity and AnimeUnity into a Jellyfin library. It handles M3U8 parsing, AES-CBC segment
decryption, parallel downloading and FFmpeg merging.

Access can be authenticated against a Jellyfin server; there is no local password store.
Authentication is **opt-in, and the choice is made once in the setup wizard**, not by an
environment variable: "Collega a Jellyfin" or "Continua senza Jellyfin", stored as the `auth_mode`
setting in `panel.db`. Until it is answered the panel serves nothing but the wizard.

## Running the Project

```bash
pip install -r requirements.txt
python main.py            # http://127.0.0.1:8000, open mode

pip install -r requirements-dev.txt   # tests only; kept out of the runtime image
pytest -q
```

**Prerequisites:** FFmpeg on PATH (the Docker image installs it). All ffmpeg invocations resolve
their binary through `app.core.ffmpeg_path.get_ffmpeg_exe()` — never call `ffmpeg`/`ffmpeg-python`
directly. Order: `FFMPEG_PATH` env override, then system PATH, then the static binary bundled by
`imageio-ffmpeg`. The fallback exists because Windows has no `apt install ffmpeg` equivalent — users
without it on PATH used to get an opaque `WinError 2` deep inside the Join step.

**Single process only.** Download job state lives in memory in `JobManager`, so running uvicorn with
`--workers > 1` or scaling the container would leave each worker blind to the others' downloads.

## Architecture

### Entry flow

`main.py` → `app.main:app`. The lifespan runs database migrations, registers the request→job
listener and starts the schedule loop. `AuthMiddleware` resolves the session cookie for every
request before any route runs.

### Layout

**`app/core/`** — source interaction and the download engine
- `page.py` — domain check, and the StreamingCommunity search. `_data_page()` /
  `fetch_page_props()` read the Inertia payload out of the server-rendered page's
  `data-page` attribute — no XHR headers, no session — and `normalize_title()` is the
  one place a raw title becomes a result. `home.py` uses all three.
- `home.py` — the shelves each source publishes on its own front page, behind a TTL
  cache keyed on `(source, host)`, so a rotation cannot reach the old domain's entry
- `domain_recovery.py` — finds the source domain again when it rotates: scrapes a
  third-party page, guards the candidate, verifies it, and *proposes* it
- `metadata.py` — plot/genres/rating/artwork/trailer, all from the title page's own props,
  behind an in-process TTL cache
- `naming.py` — the file/folder naming templates and their validation
- `film.py` — movie resolve + download; also owns `_collect_audio_tracks` / `_collect_subtitle_tracks`, which `tv.py` and `animeunity.py` import
- `tv.py` — seasons, episodes, per-episode languages, episode download
- `animeunity.py` — AnimeUnity search (`/archivio/get-animes`, paged 30 at a time,
  with the source's own `type` and `dubbed` filters), episodes, download
- `m3u8.py` — `M3U8_Parser`, `M3U8_Segments`, `M3U8_Downloader`, `Decryption`, `download_m3u8()`
- `paths.py` — destination paths (`film_path`, `episode_path`, `anime_path`). The request system's library check goes through these, so a change here changes both. Names come from `naming.py`; the optional `templates=` argument is what lets the library check ask for the *legacy* layout.
- `headers.py` — user-agent rotation, `sanitize_filename`
- `_shared.py` — embed parsing, M3U8 URL/key, `MissingAudioTrackError`, `with_retry`,
  and stream resolution (`resolve_stream`, `fetch_key_from_playlist`,
  `_FALLBACK_PROVIDERS`)

**`app/auth/`** — Jellyfin SSO, permissions, users
- `jellyfin.py` — HTTP client (MediaBrowser header, stable DeviceId, X-Forwarded-For with retry)
- `session.py` — server-side sessions (opaque token, SHA-256 stored)
- `permissions.py` — independent `IntFlag` permissions
- `deps.py` — `AuthMiddleware`, public allowlist, `require(...)`
- `router.py` / `users_router.py` — setup, login, user import and management

**`app/requests/`** — the request queue
- `models.py` — records, content key, allowed transitions
- `resolver.py` — re-resolution at approval, track verification, library check
- `service.py` — lifecycle and job wiring
- `notify.py` — dispatch; `CHANNELS` is the list every delivery fans out to
- `apprise_channel.py` — external channels (Discord, Telegram, ntfy, …) as Apprise URLs
- `router.py` — endpoints

**`app/watches/`** — followed series and anime
- `models.py` — watch records, followers, the seen-episode ledger
- `poller.py` — periodic enumeration, diff against what is seen, auto-download decision
- `router.py` — follow, unfollow, status, manual check

**`app/`** — `jobs.py` (thread pool, semaphore, SSE broadcast), `downloads_notify.py` (notifications
for downloads that skipped the queue, one summary per season/series), `downloads_hooks.py`
(post-download webhooks and the Jellyfin library refresh), `schedule.py`, `db.py`,
`config.py`, `progress.py`, `routers/`, `templates/`, `static/`

### Persistence

- `panel.db` (SQLite, stdlib `sqlite3`) — users, sessions, requests, notifications, notification
  channels, download hooks, followed series, and the `auth_mode` answer. Migrations are the ordered
  `MIGRATIONS` list in `app/db.py`, applied against `PRAGMA user_version`. Never edit an applied
  migration; append a new one. An entry is a list of SQL statements, or a `(conn, fresh)` callable
  when the decision depends on whether the database already existed — `fresh` is the only way to
  tell an upgrade from a first run, since a brand-new file reaches the last migration in the same
  pass as the first.
- `data.json` — source domain, library paths, performance settings, domain-recovery switches,
  naming templates. Anything carrying a secret goes in `panel.db` instead. Written only through
  `config.update_data()`, which holds the file lock: a background thread writes `domain`. Runtime state, not committed:
  a baked-in source domain ships stale, since the domain rotates. Tests get one from the
  `_configured_domain` autouse fixture in `tests/conftest.py`.
- `schedule.json` — scheduled downloads

Point `DB_FILE`, `DATA_FILE` and `SCHEDULE_FILE` at a persistent volume in Docker; see
`docker-compose.template.yml`.

`docs/` holds README screenshots only. It is **not** served by the app and is excluded from the
image — the favicon lives in `app/static/`. Design notes go in `design/`, likewise never served:
anything under a mounted static directory is readable by unauthenticated visitors.

### Rules that are easy to break

- **The source domain never comes from the client.** Use `app.config.configured_domain()`. Same for
  the requester's identity, which comes from the session, never from a request body.
- **Open mode has exactly one source of truth: `models.runtime_open_mode()`**, reading the
  `auth_mode` setting. It replaced an `AUTH_ENABLED` env var that was read as an import-time
  constant in three modules — so every test needed three patches, and the two switches could
  disagree: `AUTH_ENABLED=0` did not merely default to open, it made the wizard answer 404, leaving
  Settings offering a "Collega a Jellyfin" button that could not work. Do not reintroduce a
  deploy-time override. Databases predating the removal are carried over by migration v8 in
  `app/db.py`, the only place that still reads the variable.
- **Every `/api` route needs a decision**: public allowlist, `SESSION_ONLY_PATHS`, or a
  `require(...)` dependency. `tests/test_permissions.py` fails otherwise.
- **No ADMIN super-permission.** Flags are independent, so an administrator can exist who never sees
  the request queue.
- **Two caches are keyed on the source host, and both are also cleared explicitly.**
  `home` and `metadata` put the domain in the cache key, and `clear_cache()` is called
  at the only two places the domain is written — `domain_recovery.apply_candidate()`
  and `PUT /api/domain`. The key is the half that stays correct when a third write
  site is added and forgets the call; without it the panel served the old domain's
  artwork, and its now-dead image URLs, for six hours after a rotation. Not inside
  `config.update_data()`: that would make `app.config` import `app.core`.
- **StreamingCommunity's kind filter is applied per page, and the panel is built
  around that.** The source has no filter of its own and groups results by kind —
  measured, "the" answers sixty series and not one film on page 1, with the films
  further in. So a filtered page coming back empty means "none of that kind here",
  never "no more results", and the frontend keeps offering the next page while a
  filter is on. AnimeUnity filters at the source, where the offset walks the filtered
  catalogue, so there an empty page really is the end.
- **`type` is not `media_type`.** Every AnimeUnity record has `type == "anime"` and the
  whole anime flow keys off that, from the detail modal to the episodes endpoint to
  the job kind. The source's own word (Movie/TV/OVA/ONA/Special, and a long tail like
  "TV Short") rides beside it in `media_type`. Folding one into the other is what made
  every anime, films included, render as a TV series.
- **A page payload is unescaped exactly once.** BeautifulSoup decodes HTML entities in
  attribute values itself — the `data-page` on StreamingCommunity, `items-json` and
  `animes` on AnimeUnity — so there is no `html.unescape()` on top, and adding one
  would turn a plot's `&amp;amp;` into `&`.
- **Never substitute an audio track.** `strict_audio=True` on the request path turns a missing
  language into an error and parks the request for a human.
- **`max_segment_workers` is a process-wide ceiling, not a per-download target.**
  `m3u8.segment_budget()` returns one `AdaptiveLimiter` for the whole panel, held around each
  segment request. Sizing a pool per download instead multiplies it by `max_concurrent_downloads`
  and reproduces the 503 storms and container DNS exhaustion it exists to prevent. The limiter
  halves its allowance on pushback (`penalise()` on a retriable status or a connection error, once
  per `PENALTY_INTERVAL`) and climbs back one slot per allowance served (`reward()` on a 200) —
  a non-retriable status teaches it nothing, because a verdict is not congestion. `Retry-After`
  wins over the computed backoff. Segments go through the download's pooled `requests.Session`,
  never a bare `requests.get` — one DNS lookup per download instead of one per segment.
- **A library path is validated before it is stored.** A Windows path on a Linux host is accepted by
  every layer below (a backslash is a legal filename character there) and only surfaces as FFmpeg's
  "Protocol not found" after a download has finished. `paths.windows_path_problem()` guards both
  `PUT /api/domain/libraries` and the top of `_download_m3u8`; FFmpeg filenames built from a library
  root go through `ffmpeg_file_arg()`.
- **A watch never downloads anything itself.** `app/watches/poller.py` turns a new episode into an
  ordinary request and lets `service.create_request` / `service.approve` do the rest, so dedup, the
  library check and notifications keep working. Whether it is approved on the spot comes from the
  owner's live `DOWNLOAD` permission or the per-series `auto_approve` flag — never from the client.
- **Following a series seeds every episode already published.** Without that baseline the next
  cycle treats the whole back catalogue as new; an empty enumeration is treated as a read failure
  and the follow is rolled back.
- **Anything user-owned is unavailable in open mode.** The implicit user has no `jf_user` row, so a
  foreign key would fail. Check the user the middleware resolved (`is OPEN_MODE_USER`) rather than
  asking the `auth_mode` setting again: the sentinel cannot disagree with the middleware, and it
  costs no query.
- **A batch's expected total is fixed before its first job is submitted**, and every job created
  must reach a terminal listener exactly once — otherwise the batch never closes and its summary
  never fires. That is why `jobs.py` notifies listeners on *every* path out of `_run_download`,
  including the job cancelled before it started, and why `cancel()` notifies for a job still
  `scheduled` that the executor never saw.
- **External channels have no in-app recipient to wait for.** `AppriseChannel` fires with an empty
  user list on purpose: a direct download in open mode has no account, but the webhook is still the
  point. `InAppChannel` keeps the guard — with no recipients there is nothing to insert.
- **Never `confirm()`, `alert()` or `prompt()` in the frontend.** Confirmations go through
  `scConfirm()` and text entry through `scPrompt()` in `app/static/app.js`, which resolve a Promise
  from a Tabler modal. A browser dialog ignores the panel's theme, cannot be styled, and on mobile
  reads as a page-level warning rather than as part of the interface.
- **An empty `events` array on a notification channel means *every* event**, not none. Any UI
  offering a selection has to express "all" as its own state, or unchecking the last box silently
  subscribes to everything.
- **A download with a missing segment must fail, never join.** TS concatenated with gaps produces a
  file that opens, plays, and is wrong — and lands in the library looking like a good one. Failing
  is recoverable because the download can be run again; a silently truncated film is not, because
  nobody knows to. `_require_every_segment()` reads the *filesystem*, not `_failed_segments`: a run
  cut short by the watchdog never attempts the rest, so the bookkeeping is emptiest exactly when
  most of the download is absent.
- **The progress bar counts segments obtained, not attempts made.** It is also the input to the
  stall watchdog (`timer()`), so counting failures as progress does not just misreport — it stops a
  source failing every single request from ever tripping the timeout.
- **A domain found automatically is proposed, never adopted.** The page it comes from is edited by
  people we do not control, and the domain decides where every search, image fetch and download
  referer goes. `domain_recovery.is_plausible()` is the guard, and its load-bearing rule is that a
  candidate must be a **second-level domain**: checking only the first label would accept
  `streamingcommunity.attacker.tld`, where the part that decides where the traffic lands is the
  attacker's. The name pattern is a module constant with an env override and **must not become a
  settings field** — a text box that relaxes an SSRF guard is a loaded gun. `verify()` is
  deliberately stricter than `PUT /api/domain`, which accepts an empty version string: an admin
  typing a host in is making a decision, a web page is not. `domain_auto_apply` opts out of all of
  this and is off by default.
- **Title metadata has one provider and needs no credential.** It comes from the title page's own
  props — the payload already fetched to find `tmdb_id` — so it costs no request of its own. Two
  other providers were tried and removed after being measured, and the measurements are the reason:
  the site's `/api/titles/preview` endpoint answers 419 (Laravel CSRF) to every request the panel
  can make and never once worked, while TMDB turned out not to be an upgrade at all — the site
  copies its synopses from TMDB, so the text was usually identical, and the round trip *lost* a
  trailer on one title, a logo on another and 1100 characters of plot on a third. Do not re-add a
  provider without measuring it against the props on real titles first.
- **`metadata.cached_tmdb_id()` never does I/O.** A stream fallback would read it at download time,
  and a download that is about to succeed must not pay a round trip to discover a fallback it will
  not use.
- **When stream resolution has nothing to fall back to, the primary error must survive.**
  `_FALLBACK_PROVIDERS` is empty: a second road through vixsrc was built and removed, because that
  site is now a client-rendered app whose HTML carries no playlist, token or `.m3u8` at all — the
  data would have to come from reverse-engineered internal calls that break silently on any of
  their deploys. `resolve_stream()` is the seam a replacement plugs into, and the resolution
  context (`tmdb_id`, media type, season, episode) is already threaded from every caller.
  A user told "no alternative source" when the real failure was Cloudflare has been sent to debug
  the wrong thing, so the original exception is re-raised unchanged. AnimeUnity is excluded from
  all of it on purpose: no TMDB id, different embed host. `fetch_key_from_playlist()` exists so a
  new provider does not have to solve the key again — it is read from the playlist with an
  allowlisted host, never assumed.
- **Download hooks are HTTP-only, and blind.** There is no shell hook and there must not be one:
  open mode grants `MANAGE_SETTINGS` to every anonymous visitor, so a command would be RCE for
  anyone who can reach the panel. A webhook pointing at a private address is the *point* (that is
  where Jellyfin is), so the mitigation is that the response body never reaches the caller and is
  never logged — only the status code. Failure logs name the hook, never its URL.
- **Changing a naming template must not hide files already in the library.**
  `resolver.existing_file()` probes the current template *and* `naming.LEGACY_TEMPLATES`, each with
  its `.mkv` sibling; with the defaults the two render identically and it collapses back to two
  stats. In the UI a blank field means the default — its placeholder shows what that is, synced
  from `/api/domain/settings/naming-defaults` so the markup's copy cannot drift — and the frontend
  substitutes it before saving, because the server rejects an empty template outright. `naming.render()` never raises — it runs inside a download, after the bytes are fetched —
  so everything it would paper over is refused by `naming.validate()` at save time. Never
  `str.format` a user template: `{title.__class__}` leaks attributes and a stray `{` raises
  mid-download.
- Blocking work goes through `asyncio.to_thread` (routers) or the job pool. Notification channels
  are the exception by design: every caller of `notify()` is already off the loop.

### Output layout

```
<library>/
├── Movie (2020)/Movie (2020).mp4
└── Series (2019)/Season 01/Series S01E01.mp4
```

Subtitles land beside the video as `{stem}.{lang}.vtt` (Jellyfin convention). Temp segments go to
`tmp/<job_id>/` and are cleaned up afterwards.

### Quality and languages

Highest available resolution is chosen automatically (1080p → 720p → 480p → 360p). Audio and
subtitle languages are chosen per download or per request.

### vixcloud.co quirk

TV episode M3U8 URLs sometimes return 403; appending `?b=1` (or `&b=1`) resolves it. Handled in
`_fetch_text_with_b1_fallback` and `_collect_audio_tracks`.
