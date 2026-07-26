# Jellyfin SSO, Roles and Request System — Design

> **Historical document. Do not read it as a description of the current code.**
>
> Written before phase 2 of the `feat/jellyfin-sso` branch, which shipped in v2.0.0. It records the
> repo as it was *then*, the decisions taken and why, and the defects found along the way. Kept
> because the rationale is still useful; not kept up to date.
>
> In particular, **every defect in §2 has since been fixed** — including the two SSRFs, which are
> the ones that matter if you are skimming. The source domain is read server-side through
> `app.config.configured_domain()` (`app/routers/downloads.py`), the image proxy no longer takes a
> host from the caller (`app/routers/images.py`), and `sanitize_filename` is a real implementation
> (`app/core/headers.py`). `tests/test_security.py` covers them. For how the panel works now, read
> `README.md` and `CLAUDE.md`.

---

## 1. The repo as it was

*(At the time of writing. `CLAUDE.md` then still described a CLI under `Src/` with `run.py`, code
that had already been removed; it now describes the web panel.)*

| Area | State |
|---|---|
| Framework | FastAPI + uvicorn, started from `main.py` (`python main.py`, **single process**, no `workers`) |
| ORM / migrations | **None.** No database dependency in `requirements.txt` |
| Persistence | `data.json` (domain, libraries, settings) guarded by `filelock`; `schedule.json` via `app/schedule.py`; job state **in memory only** |
| Auth / users | **None.** No session, cookie or login anywhere |
| Frontend | Server-rendered: one Jinja template `app/templates/index.html` (~1700 lines, inline CSS) plus `app/static/app.js` (~1600 lines, vanilla JS, no build step). Tabler 1.4 + Tabler Icons from CDN |
| Config / secrets | Environment variables only, in `app/config.py` (`VIDEOS_DIR`, `HOST`, `PORT`, `DATA_FILE`, `SCHEDULE_FILE`, `TMP_DIR`). No secrets today |
| Tests | **None.** No pytest, no linter, no test CI |

### Search → resolve → download flow

1. `GET /api/search` (`app/routers/search.py`) → `app/core/page.py:search()` or
   `app/core/animeunity.py:search()`.
2. Clicking a card opens the detail modal (`app/static/app.js:openDetailModal`), which calls
   `GET /api/search/languages/{id}`. That endpoint really resolves the iframe and the master M3U8 and
   returns `{audio: [...], subtitles: [...]}`. The user ticks the audio/subtitle checkboxes.
3. `POST /api/download/film|episode|anime` (`app/routers/downloads.py`) carries
   `audio_languages` / `subtitle_languages` into `job_manager.submit_*`.
4. `app/jobs.py` — `JobManager` with a `ThreadPoolExecutor(64)` and a
   `BoundedSemaphore(max_concurrent_downloads)`. Progress is pushed over a global SSE stream,
   `GET /api/progress/stream`.
5. `download_film` / `download_episode` / `download_anime_episode` collect the audio and subtitle
   tracks, then hand off to `download_m3u8()` in `app/core/m3u8.py`.

**Consequence for the request system:** available tracks are known only *after* a full resolve of the
source. The requester already sees the real options today, because the detail modal performs that
resolve before the download button is pressed. The request flow reuses the same endpoint and stores
the result as a snapshot.

### Existing async machinery

`JobManager` (thread pool + semaphore) plus an `asyncio` scheduler loop that fires due jobs every 30
seconds, backed by the durable JSON `ScheduleStore` and re-hydrated at startup. The request system
**reuses this**; no second queue is introduced.

---

## 2. Problems found in the existing code

**All fixed as of v2.0.0 — see the note at the top of this file.** Listed here as they were found:
reported rather than replicated, with fixes landing in the phase that touched them, or in phase 7.

| # | Where | Problem |
|---|---|---|
| 1 | `app/routers/downloads.py` — every request body | `domain` is supplied by the **client**. An authenticated low-privilege user can make the server issue HTTP requests to an arbitrary host: SSRF. It must be read server-side from `data.json` |
| 2 | `app/routers/images.py:45` | `/api/image/{domain}/{path}` is an open proxy — `domain` is unvalidated and unconstrained. SSRF |
| 3 | `app/core/film.py:74`, `app/core/tv.py:111` | When a requested audio language is not present, `_collect_audio_tracks` logs a warning and carries on. The download then silently produces a file with the wrong audio — exactly the substitution the spec says must never happen automatically |
| 4 | `app/jobs.py:319` | `created_at=datetime.utcnow()` is naive while `scheduled_at` is timezone-aware; comparisons and serialisation are inconsistent |
| 5 | `app/jobs.py:71` | `update_max_concurrent` swaps the semaphore while running jobs still hold the old one, so the configured limit is transiently exceeded |
| 6 | `app/jobs.py:249` | `asyncio.run_coroutine_threadsafe(..., self._loop)` with `self._loop` possibly `None` |
| 7 | `app/jobs.py:50` | Job state lives only in memory: running with `--workers > 1` is **already broken today** (jobs are invisible across workers). The single-process constraint gets documented, and the request path claims work atomically in SQLite so a multi-worker deployment cannot execute the same download twice |
| 8 | `app/core/headers.py:9` | `sanitize_filename` is a **no-op on Linux**, yet the Docker image is Linux. Titles come from third-party sites and end up in filesystem paths |

Blocking I/O is handled correctly where it matters: routers wrap synchronous work in
`asyncio.to_thread` (`app/routers/search.py`, `app/routers/domain.py`) and downloads run in the thread
pool. Jellyfin calls follow the same convention.

---

## 3. What was taken from Seerr

Behaviour was studied, no code was copied.

- `Authorization: MediaBrowser Client=…, Device=…, DeviceId=…, Version=…, Token=…` header.
- Login through `POST /Users/AuthenticateByName` with `{Username, Pw}`.
- First-admin bootstrap gated on `User.Policy.IsAdministrator`.
- Application API key created with `POST /Auth/Keys?App=<name>`, then read back from `GET /Auth/Keys`.
- Identity key is `User.Id`, not the username — Seerr looks users up by `jellyfinUserId`.
- DeviceId derived from the user and reused from the stored record, so Jellyfin keeps one device per
  panel user instead of one per login.
- `X-Forwarded-For` carrying the real client IP, **with a retry without the header on failure**. That
  fallback exists because Jellyfin rejects the request when the proxy is not among its `KnownProxies`,
  and because the LDAP plugin applies network restrictions to the forwarded address. The retry is
  replicated *and* a warning is logged, so an administrator can tell why user IPs stopped propagating
  instead of silently seeing the panel's own address.
- A "new Jellyfin users may sign in" flag, off by default.

### Deliberate divergence

In Seerr, `Permission.ADMIN` short-circuits every permission check. Here it does not, and there is no
super-flag at all: the spec explicitly requires administrators who manage the request queue to coexist
with administrators who cannot see it.

---

## 4. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Persistence | SQLite through the stdlib `sqlite3`, WAL mode, schema versioned with `PRAGMA user_version` | No new dependency and no new service to install, which matters for non-expert self-hosters. Transactions, a UNIQUE index for deduplication and immediate revocation are all required; JSON files offer no atomic compare-and-set. `data.json` and `schedule.json` are left untouched |
| Sessions | Opaque token (`secrets.token_urlsafe(32)`), **SHA-256 digest stored** in the database, delivered as an `HttpOnly`, `SameSite=Lax` cookie | JWTs are convenient but **cannot be revoked**. Disabling a user has to take effect immediately, which server-side sessions give by deleting rows |
| Permissions | Bitwise `IntFlag`, independent flags, **no ADMIN super-flag** | Required so that some administrators never see the request queue |
| Enforcement | A `require(Permission.X)` dependency factory attached to routers/routes, plus a test that walks `app.routes` and fails when an `/api/` route is neither in the public allowlist nor guarded | "Systematically, not case by case" |
| CSRF | `SameSite=Lax` plus a double-submit token (`X-CSRF-Token`, held in the session row) on every non-GET | Same-origin cookie-authenticated frontend. SSE uses `EventSource`, which is GET and cannot set headers |
| Async execution | Reuse `JobManager`; durability in the `jf_request` table; work claimed with `UPDATE … WHERE status='approved'` and a rowcount check | No Celery or Redis. The atomic claim makes a multi-worker deployment harmless on the request path |
| User Jellyfin tokens | **Not stored.** Used only for the duration of the login request, then invalidated with `POST /Sessions/Logout` | Storing more than necessary is free attack surface. Only the service API key is persisted |
| Identity | `jellyfin_user_id` UNIQUE; the username is only a display cache, refreshed at every login | Jellyfin usernames can be renamed |
| DeviceId | Deterministic `base64("SCPanel_" + jellyfin_user_id)`, written once and reused | An unstable identifier fills the Jellyfin dashboard with phantom devices and can hit per-account device limits |
| "Fix the link" | An approver may re-bind a request to a different search result (`external_id` / `slug` / `season` / `episode`) and correct the tracks, then retry. **No free-form M3U8 URL** | An arbitrary URL from a form would be SSRF. The real-world case — the source domain changed — is already covered by re-resolving at approval time |

### Code layout

New code lives in sub-packages of the existing application, `app/auth/` and `app/requests/`, and
reuses `app/core/` and `app/jobs.py` unchanged. Forking `app/` into a parallel package would mean
forking the resolver and the downloader, which the spec forbids.

Isolation from production is done where things actually get overwritten: a separate branch, a separate
compose file, and a GHCR image suffixed `-jellyfin`, so `…/streamingcommunity-downloader:latest` is
never touched.

---

## 5. Request lifecycle

```
                 ┌──────────────┐
   POST /requests│  available   │  file already in the library
        ─────────┤──────────────┘
                 │
                 ▼
            ┌─────────┐  approve   ┌──────────┐  claim   ┌─────────────┐
            │ pending ├───────────►│ approved ├─────────►│ downloading │
            └────┬────┘            └────┬─────┘          └──────┬──────┘
                 │ deny                 │ dead link /           │
                 │                      │ missing track         │
                 ▼                      ▼                       ▼
            ┌────────┐          ┌─────────────────┐      ┌───────────┐
            │ denied │◄─────────┤ needs_attention │      │ completed │
            └────────┘   deny   └────────┬────────┘      │  failed   │
                                         │ fix + retry   └───────────┘
                                         └──────────────► approved
```

Terminal states: `available`, `completed`, `denied`, `failed`, `cancelled`. Transitions are applied as
`UPDATE … WHERE id = ? AND status = ?` and validated against an explicit table, so concurrent
approvals cannot both win.

**Deduplication.** `content_key = sha256(source | media_type | external_id | season | episode |
sorted audio | sorted subtitles)`. The chosen tracks are part of the key, so the same film requested
with different audio is genuinely two requests. On collision with an open request, a row is added to
`jf_request_subscriber` and every subscriber is notified.

**Approval does real work.** Days can pass between request and approval, so approval re-resolves the
source using the *current* domain and freshly fetched tokens. A dead link, or a requested audio track
that is no longer available, moves the request to `needs_attention` and notifies the approvers. There
is no automatic substitution: a human decides.

**Notifications** are in-app only. They are emitted through a single `Notifier` that iterates over a
list of channels, of which `InAppChannel` is currently the only one. Adding a channel later means
appending to that list, not touching business logic.

---

## 6. Phases

1. Repo summary and decisions — this document.
2. Jellyfin authentication: SQLite store, `JellyfinClient`, server-side sessions, setup and login.
3. Independent permission flags and systematic endpoint guards.
4. Jellyfin user import and permission management.
5. Requests, approval, asynchronous execution and in-app notifications.
6. Seerr-style interface built from Tabler components, keeping the current palette.
7. Security pass, packaging under the `-jellyfin` name, and tests.
