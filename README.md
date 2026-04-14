# StreamingCommunity Downloader

A Python 3.11+ tool to download films and TV series from the StreamingCommunity platform.  
Comes with both a **CLI** and a **web panel** (FastAPI + Tabler UI).

---

## Features

- Search films and TV series via the StreamingCommunity API
- Download movies and individual episodes or entire seasons
- Automatic highest-quality selection (1080p → 720p → 480p → 360p)
- AES-CBC segment decryption for HLS streams
- Parallel segment download (up to 150 concurrent workers)
- Subtitle download for non-Italian audio tracks (`.vtt`)
- Alternate audio track merge via FFmpeg
- Auto-install FFmpeg on first run (Windows/Linux)
- Web panel with real-time download progress via SSE
- File manager with drag-and-drop, streaming, and library support
- Docker + NFS ready

---

## Usage — Web Panel (recommended)

### With Docker (no clone required)

Download the stack template and start:

```bash
curl -O https://raw.githubusercontent.com/EdoardoFiore/StreamingCommunity-downloader/main/docker-compose.template.yml
# edit the volume device path, then:
docker compose -f docker-compose.template.yml up -d
```

The panel is available at `http://localhost:8000`.

### From source

```bash
git clone https://github.com/EdoardoFiore/StreamingCommunity-downloader.git
cd StreamingCommunity-downloader
pip install -r requirements.txt
python main.py
```

---

## Usage — CLI

**Requirements:** Python ≥ 3.11, FFmpeg (auto-installed on first run)

```bash
pip install -r requirements.txt
python run.py
```

**Auto-update:**

```bash
python update.py
```

### Selection syntax

| Syntax | Meaning |
|--------|---------|
| `1` | Single episode/season |
| `[1-5]` | Range |
| `[1,3,7]` | Discontinuous list |
| `*` | All |

---

## Configuration

The domain is stored in `data.json` and can be set via the web panel settings or on first CLI run.

| Env variable | Default | Description |
|---|---|---|
| `VIDEOS_DIR` | `videos/` | Output directory for downloaded files |
| `HOST` | `127.0.0.1` | Web panel bind address |
| `PORT` | `8000` | Web panel port |
| `DATA_FILE` | `data.json` | Domain + library config file |
| `TMP_DIR` | `tmp/` | Temp directory for segment download |

---

## Output structure

```
videos/
├── MovieTitle.mp4
└── SeriesTitle/
    ├── S01E01.mp4
    └── S01E02.mp4
```

---

## Architecture

```
run.py / main.py
├── Src/Api/          — Search, film and TV series metadata
├── Src/Lib/FFmpeg/   — HLS download engine (parse → decrypt → concat → merge)
├── Src/Util/         — Console, headers, user-agent rotation
└── app/              — FastAPI web panel
    ├── core/         — Web equivalents of Src/Api (film, tv, m3u8, page)
    ├── routers/      — REST endpoints (search, downloads, files, progress, domain)
    ├── jobs.py       — Download job queue and state machine
    ├── progress.py   — SSE broadcast for live progress
    ├── static/       — Frontend (app.js, CSS)
    └── templates/    — Jinja2 HTML templates
```

---

## Docker

The image is published automatically to GitHub Container Registry on every push to `main`:

```
ghcr.io/edoardofiore/streamingcommunity-downloader:latest
```

See [docker-compose.template.yml](docker-compose.template.yml) for a ready-to-use stack.

---

## License

MIT
