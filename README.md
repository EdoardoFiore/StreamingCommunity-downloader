<p align="center">
  <img src="docs/banner.png" alt="StreamingCommunity Downloader" width="520"/>
</p>

<p align="center">
  A self-hosted web panel to download films and TV series from the StreamingCommunity platform.<br/>
  Built with FastAPI, real-time progress via SSE, and an integrated file manager.
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
- **Login with Jellyfin credentials** — no second account, no local password store
- **Independent permissions** — download directly, request, approve, manage users, manage settings
- **Request queue with approval**, preserving the audio and subtitle tracks the requester chose
- Request status shown on the search result cards, and in-app notifications
- Automatic quality selection (1080p → 720p → 480p → 360p)
- Parallel HLS segment download with AES-CBC decryption
- Multi-audio track merge via FFmpeg
- Subtitle download (`.vtt`) for non-Italian audio tracks
- Real-time download progress with per-phase steps (video → audio → merge)
- Integrated file manager with drag-and-drop and video streaming
- Jellyfin library path configuration
- Scheduled downloads
- Docker ready

---

## Quick Start

### Docker (recommended)

```bash
curl -O https://raw.githubusercontent.com/EdoardoFiore/StreamingCommunity-downloader/main/docker-compose.template.yml
# Edit the two volume paths — and create the config one — then:
docker compose -f docker-compose.template.yml up -d
```

The panel is available at `http://localhost:8000`. On first start it asks for the Jellyfin server
URL and the credentials of a **Jellyfin administrator**, who becomes the panel administrator. There
is no API key to paste and no credentials in the compose file.

**No Jellyfin server?** Press **Continua senza Jellyfin** on that screen. The panel then runs with no
login at all — direct download, settings and the file manager — and you can connect Jellyfin later
from **Impostazioni → Accesso e utenti**, without a restart. Setting `AUTH_ENABLED=0` (or leaving the
variable out) does the same thing at deploy time.

The image is published to GitHub Container Registry:

```
ghcr.io/edoardofiore/streamingcommunity-downloader:latest
```

Set the **source domain** in **Impostazioni** after the first start: it is not baked into the image,
because it rotates.

### Upgrading from v1

v1 is the panel before Jellyfin authentication existed. If you are running it, two things matter:

- **Nothing breaks by default.** `AUTH_ENABLED` defaults to off, and your existing compose file does
  not set it, so pulling the new image keeps the panel exactly as open as it was. You will notice
  the redesigned interface, and you will have to re-enter the source domain once (see above).
- **Add a config volume before enabling authentication.** Users, sessions, requests and the auth
  choice live in `panel.db`. Without the `/app/config` volume and the `DB_FILE`/`DATA_FILE`/
  `SCHEDULE_FILE` variables from `docker-compose.template.yml`, that file sits inside the container
  and is destroyed on every re-pull — the setup wizard would come back every time.

To stay on v1 instead, pin the tag and stop pulling `latest`:

```yaml
image: ghcr.io/edoardofiore/streamingcommunity-downloader:1.0.0
```

Its compose file remains available at
`https://raw.githubusercontent.com/EdoardoFiore/StreamingCommunity-downloader/v1.0.0/docker-compose.template.yml`.

If you were using the `-jellyfin` image from the feature branch, change the `image:` line to the one
above — that package is no longer built. Everything else in your compose file stays as it is.

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

---

## Users, roles and requests

The first sign-in configures the panel and creates its administrator; only a Jellyfin administrator
can do it. After that, Jellyfin accounts **do not** get access automatically — an administrator
imports them from **Utenti** and assigns permissions. Opening the panel to every Jellyfin account is
a switch on that page, off by default.

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
the wrong thing. Two people asking for the same content with the same tracks share one download and
are both notified; different audio makes two distinct requests.

Disabling a user takes effect on their next request and keeps their history.

---

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `VIDEOS_DIR` | `videos` | download destination |
| `DB_FILE` | `panel.db` | users, sessions, requests — **put this on a persistent volume** |
| `DATA_FILE` | `data.json` | source domain, libraries, performance settings |
| `SCHEDULE_FILE` | `schedule.json` | scheduled downloads |
| `TMP_DIR` | `tmp` | working directory for HLS segments, cleaned up after each job |
| `HOST` / `PORT` | `127.0.0.1` / `8000` | bind address |
| `AUTH_ENABLED` | `0` | set to `1` to enable Jellyfin login, requests and users — see below |
| `COOKIE_SECURE` | `0` | set to `1` when serving over HTTPS |
| `COOKIE_SAMESITE` | `lax` | set to `none` (with `COOKIE_SECURE=1`) only to embed the panel in an iframe on another site/scheme — see below |
| `TRUST_PROXY_HEADERS` | `0` | set to `1` only behind a reverse proxy you control |

`panel.db` holds the Jellyfin service API key: keep it out of any web-served directory and off
world-readable storage.

**Run one process.** Download state is held in memory by a single process, so do not add
`--workers` or scale the service — a second replica would neither see nor report on the first's
downloads.

### Running without Jellyfin

Two ways, and they end up in the same place — no login, no setup wizard, no request queue, no user
management. Every visitor gets the same implicit access: direct download, settings and the file
manager, the surface the panel had before SSO existed.

- **`AUTH_ENABLED=0`, or the variable left out entirely** — the default. A deploy-time choice, which
  is also what makes an upgrade from v1 a no-op.
- **`AUTH_ENABLED=1` and then "Continua senza Jellyfin"** on the setup screen. Same result, chosen at
  runtime and recorded in `panel.db`, so it needs the config volume to survive a re-pull. Connect
  Jellyfin later from **Impostazioni → Accesso e utenti**; it authenticates a Jellyfin administrator,
  creates the panel's own API key and signs you in. Every other session that was open at that moment
  is signed out, since the panel stops being open.

Going the other way — from a connected Jellyfin back to open mode — is deliberately not supported in
the UI: the imported users and their permissions would be left in an ambiguous state. Flipping
`AUTH_ENABLED` from `1` to `0` on a live deployment does stop enforcing the permissions of users who
already exist, so decide once, before rollout.

`panel.db` is created either way (it also backs the schedule and job bookkeeping) but stays empty of
users while the panel is open.

### Embedding the panel in an iframe (e.g. a Jellyfin custom tab)

A `SameSite=Lax` session cookie — the default, correct for every normal deployment — is not sent on
requests made from inside a cross-site iframe. Framed on a different domain, port-with-different-scheme,
or behind a different reverse proxy than the panel's own origin, every request inside the frame looks
logged out, and login bounces straight back to itself: an apparent infinite loop.

Two ways to fix it:

- **Reverse-proxy the panel under the same site and scheme as the embedding page** (e.g. as a subpath of
  your Jellyfin domain). No configuration change needed — the iframe stops being cross-site.
- **Set `COOKIE_SAMESITE=none`**, together with `COOKIE_SECURE=1` — the panel must be served over HTTPS,
  since browsers reject a `SameSite=None` cookie that isn't also `Secure`. This is safe to relax: CSRF
  protection here does not depend on `SameSite` — every state-changing request already carries a
  double-submit token no outside origin can read.

Chrome (and Chromium-based browsers) separately enforce **Private Network Access**: a page loaded from a
public address cannot embed a frame that resolves to a private/LAN address, and vice versa. Both the
Jellyfin page and the panel need to resolve to addresses in the *same* tier — both public or both
private — from the browser's point of view, or the iframe fails with "the connection was blocked".

### Skipping the login screen inside the custom tab

Since the panel is already embedded in a page where Jellyfin itself is signed in, the visitor should not
have to log in a second time. `POST /api/auth/jellyfin-token` trades an already-issued Jellyfin access
token for a panel session — the panel's own login page listens for one over `postMessage` and, if it gets
one, skips straight past the login form.

The token has to come from the parent frame, since that's the only place it exists: add this to the same
custom tab HTML that holds the iframe (Jellyfin's own page, not the panel), replacing `PANEL_ORIGIN` with
the panel's exact origin:

```html
<script>
(function () {
  var PANEL_ORIGIN = 'https://request.edoardo-fiore.it'; // ← your panel's origin, no trailing slash
  window.addEventListener('message', function (event) {
    if (event.origin !== PANEL_ORIGIN) return;
    if (!event.data || event.data.type !== 'sc-panel-ready') return;
    try {
      var token = window.ApiClient.accessToken();
      if (token) event.source.postMessage({ type: 'sc-panel-jellyfin-token', token: token }, PANEL_ORIGIN);
    } catch (e) { /* ApiClient not ready yet */ }
  });
})();
</script>
```

The token is never trusted blindly: the panel checks it live against the configured Jellyfin server
before issuing a session, exactly as it would a password. Nothing changes for a Jellyfin user who has not
been imported into the panel, or who visits the panel outside the iframe — they still see the normal
login form.

---

## Output structure

```
videos/
├── MovieTitle/
|    └── MovieTitle.mp4
└── SeriesTitle/
    └── Season 01
        ├── S01E01.mp4
        └── S01E02.mp4
```

---

## License

MIT
