<p align="center">
  <img src="docs/banner.png" alt="StreamingCommunity Downloader" width="520"/>
</p>

<p align="center">
  A self-hosted web panel to download films, TV series and anime into a Jellyfin library.<br/>
  Request queue, followed series, notifications to Discord and Telegram, and an integrated
  file manager.
</p>

---

## Screenshots

<table>
  <tr>
    <td><img src="docs/search.png" alt="Search"/></td>
    <td><img src="docs/serie-detail.png" alt="Series detail"/></td>
  </tr>
  <tr>
    <td><img src="docs/episode-list.png" alt="Episode list"/></td>
    <td><img src="docs/file-manager.png" alt="File manager"/></td>
  </tr>
  <tr>
    <td colspan="2"><img src="docs/settings.png" alt="Settings" width="50%"/></td>
  </tr>
</table>

---

## Features

- Search and download films, TV series, and anime
- Automatic quality selection (1080p → 720p → 480p → 360p)
- Parallel HLS segment download with AES-CBC decryption
- Multi-audio track merge via FFmpeg
- Subtitle download (`.vtt`) for non-Italian audio tracks
- Real-time download progress with per-phase steps (video → audio → merge)
- Integrated file manager with drag-and-drop, video streaming and free space on the media volume
- Jellyfin library path configuration
- Scheduled downloads
- **Follow a series or anime** and get new episodes as the source publishes them
- **Notifications to Discord, Telegram, ntfy and anything else Apprise speaks**, with per-channel
  event selection
- One notification per download — and a single summary for a whole season or series
- Optional login with Jellyfin credentials — no second account, no local password store
- Independent permissions: download directly, request, approve, manage users, manage settings
- Request queue with approval, preserving the audio and subtitle tracks the requester chose
- Request status on the search result cards, and an in-app notification bell
- Docker ready

---

## Quick Start

### Docker (recommended)

```bash
curl -O https://raw.githubusercontent.com/EdoardoFiore/StreamingCommunity-downloader/main/docker-compose.template.yml
# Edit the volume paths — create the config one — then:
docker compose -f docker-compose.template.yml up -d
```

The panel is available at `http://localhost:8000`. Set the source domain in **Impostazioni** on first
use: it is not shipped in the image, because it rotates.

The image is published to GitHub Container Registry on every push to `main`:

```
ghcr.io/edoardofiore/streamingcommunity-downloader:latest
```

### Trying a branch before it is released

Every push to a branch other than `main` publishes to a **separate** package, so a work-in-progress
build can never be pulled by a deployment pointing at the release image:

```
ghcr.io/edoardofiore/streamingcommunity-downloader-dev:dev          # the latest branch build
ghcr.io/edoardofiore/streamingcommunity-downloader-dev:my-branch    # that branch only
ghcr.io/edoardofiore/streamingcommunity-downloader-dev:sha-abc1234  # one exact commit
```

Tests have to pass first: a red `pytest -q` publishes nothing.

```bash
curl -O https://raw.githubusercontent.com/EdoardoFiore/StreamingCommunity-downloader/main/docker-compose.dev.template.yml
# Point panel_config_dev at a NEW directory, then:
docker compose -f docker-compose.dev.template.yml up -d
```

It listens on `http://localhost:8001` so it can run alongside the release panel. Give it its own
config directory: **database migrations only run forwards**, so pointing a dev image at the
production config volume upgrades the real database with no way back.

### From source

```bash
git clone https://github.com/EdoardoFiore/StreamingCommunity-downloader.git
cd StreamingCommunity-downloader
pip install -r requirements.txt
python main.py

pip install -r requirements-dev.txt   # tests only
pytest -q
```

**Prerequisites:** Python ≥ 3.11, FFmpeg.

### Upgrading from v1

v1 is the panel before the Jellyfin login existed. **Pulling the new image changes nothing by
default**: `AUTH_ENABLED` is off unless you set it, so the panel stays as open as it was. You will
see the redesigned interface, and you have to set the source domain once (see above).

To turn the login on, copy the `/app/config` volume and the `DB_FILE` / `DATA_FILE` /
`SCHEDULE_FILE` variables from `docker-compose.template.yml`, then set `AUTH_ENABLED=1`. That volume
is not optional: users, sessions and requests live in `panel.db`, which without it sits inside the
container and is lost on every pull.

To stay on v1, pin the tag instead of following `latest`:

```yaml
image: ghcr.io/edoardofiore/streamingcommunity-downloader:1.0.0
```

If you were running the `-jellyfin` image from the development branch, change the `image:` line to
the main one above — that package is frozen and no longer built. Nothing else changes: the variables
and the `/app/config` volume are the same, so `panel.db` and your users carry over as they are.

---

## Users, roles and requests

With `AUTH_ENABLED=1`, the first sign-in configures the panel and creates its administrator; only a
Jellyfin administrator can do it. After that, Jellyfin accounts **do not** get access automatically —
an administrator imports them from **Utenti** and assigns permissions. Opening the panel to every
Jellyfin account is a switch on that page, off by default.

Permissions are independent flags, not a ladder:

| Permission | Grants |
|---|---|
| `DOWNLOAD` | start and schedule downloads directly |
| `REQUEST` | search and create requests |
| `MANAGE_REQUESTS` | see the queue, approve, deny, fix |
| `MANAGE_USERS` | import users, assign permissions, disable accounts |
| `MANAGE_SETTINGS` | source domain, libraries, performance |
| `MANAGE_FILES` | move, rename and delete in the file manager |
| `VIEW_LIBRARY` | browse and stream the library |

There is deliberately no "admin" flag that implies the rest, so you can have an administrator who
manages settings and users but never sees the request queue.

A user without `DOWNLOAD` sees the same form and the same audio and subtitle checkboxes; the button
says **Richiedi** and creates a request. On approval the source is re-resolved against the current
domain — a dead link or a missing audio track parks the request for a human instead of downloading
the wrong thing.

Two people asking for the same content with the same tracks share one download and are both
notified. Asking for **different** tracks makes two distinct requests, because merging them would
give one of them the wrong file — but they resolve to the same path in the library, so the download
fetches the union of what everyone asked for. Two people wanting one film in Italian and English get
one file carrying both, and the player picks. A request whose tracks are already in the file is
satisfied without downloading anything: the check reads the file's audio and subtitle streams, not
its name.

### Running without Jellyfin

Leave `AUTH_ENABLED` unset (or `0`) and the panel runs with no login at all: every visitor gets
direct download, settings and the file manager. With `AUTH_ENABLED=1` you get the same result by
pressing **Continua senza Jellyfin** on the setup screen, and can connect Jellyfin later from
**Impostazioni → Accesso e utenti** without a restart.

Going back — from a connected Jellyfin to no login — is deliberately not offered in the UI: the
imported users and their permissions would be left in an ambiguous state.

---

## Following a series

Any series or anime can be **followed** from its page. From then on the panel checks the source
periodically and picks up new episodes as they are published. Following means *from here on*:
everything already released is recorded as seen, so you do not get eight seasons queued the next
morning.

What happens to a new episode depends on the permissions of whoever follows it:

- someone who **can download** finds it in the library on its own;
- someone who **can only request** produces an ordinary request for an approver to accept.

In the second case the approver is told **when the series is followed**, not weeks later when an
episode finally appears, and can **approve the series once**: from then on its new episodes go
straight through, and only that series is affected. The choice is on the *Serie seguite* page, or on
the "Auto i prossimi" checkbox of the request itself.

A watch never downloads anything by itself — it creates a normal request and lets the queue, the
library check and the notifications do their work, so nothing is duplicated and nothing is silently
substituted. **Controlla ora** forces a check immediately instead of waiting for the next cycle; the
interval is in **Impostazioni → Download**.

The *Serie seguite* page lists what you follow, when it was last checked, and whether it downloads
automatically or goes through the queue. An approver also sees everyone else's, with who follows
each one.

---

## Notifications

The panel notifies in two directions.

**In-app**, the bell holds request and download events for the signed-in user, and they can be
marked read or deleted, one at a time or all at once. On a panel running without Jellyfin the bell
belongs to the panel itself, so an open installation still sees its own downloads.

**Outward**, any number of channels can be configured in **Impostazioni → Notifiche** by pasting an
[Apprise](https://github.com/caronc/apprise/wiki) URL — Discord, Telegram, ntfy, Gotify, Slack,
email, and everything else Apprise supports. No key in the compose file, no restart. Each channel
chooses **which events it wants**; selecting none means all of them. Messages carry a title and are
coloured by outcome where the service supports it, and errors have their query strings stripped
before they leave the panel, since external channels are somebody else's servers.

Channels are **global, not per user**: whoever configures one decides what reaches everybody reading
it.

Download notifications are grouped by the action that caused them — one for a film or a single
episode, and a **single summary** for a whole season, series or anime, listing any episodes that
failed and why.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `8000` | bind address |
| `VIDEOS_DIR` | `videos` | download destination |
| `DB_FILE` | `panel.db` | users, sessions, requests — **put this on a persistent volume** |
| `DATA_FILE` | `data.json` | source domain, libraries, performance settings |
| `SCHEDULE_FILE` | `schedule.json` | scheduled downloads |
| `TMP_DIR` | `tmp` | HLS segments while a job runs, cleaned up afterwards |
| `FFMPEG_PATH` | — | full path to `ffmpeg`, when it is not on `PATH` |
| `FFPROBE_PATH` | — | full path to `ffprobe`; see the note below |
| `AUTH_ENABLED` | `0` | `1` enables Jellyfin login, requests and users |
| `COOKIE_SECURE` | `0` | set to `1` when serving over HTTPS |
| `COOKIE_SAMESITE` | `lax` | `none` (with `COOKIE_SECURE=1`) only to embed the panel cross-site |
| `TRUST_PROXY_HEADERS` | `0` | set to `1` only behind a reverse proxy you control |

The source domain and the Jellyfin library paths are configurable from **Impostazioni** in the UI.

**FFmpeg and ffprobe.** `ffmpeg` is required; the Docker image installs it. Without one on `PATH`
the panel falls back to the static binary bundled with `imageio-ffmpeg`, which is what makes it work
on Windows. `ffprobe` is optional but recommended: it is what lets the panel read which audio and
subtitle tracks a file in the library already carries, so a request for a track that is missing is
not mistaken for one that is already satisfied. Debian's `ffmpeg` package ships both, so the image
has it; where it is absent the panel treats an existing file as satisfying the request and says so
in the log.

`panel.db` holds the Jellyfin service API key: keep it out of any web-served directory and off
world-readable storage.

**Run one process.** Download state is held in memory by a single process, so do not add
`--workers` or scale the service — a second replica would neither see nor report on the first's
downloads.

### Embedding as a Jellyfin custom tab

Point an iframe at the panel; nothing else is required. Sign in once inside the tab and stay signed
in — the session lasts **30 days** and renews on every visit, so in practice you log in once and
forget about it.

With Jellyfin and the panel on subdomains of the same domain over HTTPS — say `jf.example.com` and
`request.example.com` — the frame is same-site and the session cookie is sent inside it with the
default `COOKIE_SAMESITE=lax`. On different domains, or different schemes, the frame is cross-site
and a `Lax` cookie is dropped silently: every request inside the frame looks logged out and login
bounces back on itself, which reads as an infinite loop. Either set `COOKIE_SAMESITE=none` together
with `COOKIE_SECURE=1` — browsers reject a `SameSite=None` cookie that is not also `Secure` — or
reverse-proxy the panel under the same site as Jellyfin.

Chromium browsers separately enforce Private Network Access: the Jellyfin page and the panel must
both resolve to public addresses, or both to private ones, from the browser's point of view. Mixing
tiers fails with "the connection was blocked".

`POST /api/auth/jellyfin-token` trades an already-issued Jellyfin access token for a panel session,
for skipping even that one login. It needs a script running on the Jellyfin page, which posts the
token to the frame. Do not put that script in the custom tab HTML: tab content is injected as a
string, so quoting breaks the page, and `<script>` tags inserted that way never execute.

---

## Output structure

```
videos/
├── Movie (2020)/
│   ├── Movie (2020).mkv
│   └── Movie (2020).en.vtt
└── Series (2019)/
    └── Season 01/
        ├── Series S01E01.mkv
        └── Series S01E02.mkv
```

The extension is `.mp4` for a single audio track and `.mkv` once there is more than one, which is
what carrying several languages in one file requires. Subtitles are embedded and also written beside
the video as `{name}.{lang}.vtt`, the layout Jellyfin expects.

---

## License

MIT
