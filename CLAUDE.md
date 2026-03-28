# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

StreamingCommunity_api is a Python 3.11+ CLI tool that downloads films and TV series from the StreamingCommunity platform. It handles M3U8 stream parsing, AES-CBC segment decryption, parallel downloading, and FFmpeg-based media merging.

## Running the Project

```bash
# Install dependencies
pip install -r requirements.txt

# Run the downloader (main entry point)
python run.py

# Run the auto-updater
python update.py
```

**Prerequisites:** FFmpeg is required — auto-installed on first run if missing (downloaded from gyan.dev).

There is no test suite, linter configuration, or build system.

## Architecture

### Entry Flow

`run.py` → `initialize()` (Python version check, FFmpeg verify, banner) → `main()` loop:
1. Load domain from `data.json` (or prompt user)
2. Search via `Src/Api/page.py:search()` → `GET /api/search`
3. User selects result → route to `Src/Api/film.py` (movie) or `Src/Api/tv.py` (TV series)
4. Both converge on `Src/Lib/FFmpeg/my_m3u8.py:M3U8_Downloader`

### Key Modules

**`Src/Api/`** — Platform interaction
- `page.py` — Domain management (`data.json`), search API
- `film.py` — Movie download: extracts iframe → parses JS metadata → builds M3U8 URL
- `tv.py` — TV series: fetches seasons/episodes, loops `dw_single_ep()` per episode

**`Src/Lib/FFmpeg/`** — Core download engine
- `my_m3u8.py` — The main download pipeline:
  - `M3U8_Parser`: Parses playlists (segments, subtitles, alternate audio)
  - `M3U8_Segments`: Downloads `.ts` segments in parallel (up to 150 `ThreadPoolExecutor` workers) with AES-CBC decryption
  - `M3U8_Downloader`: Orchestrates parse → download → FFmpeg concat → optional audio merge
  - `Decryption`: AES-CBC with IV extracted from M3U8 `#EXT-X-KEY`
- `installer.py` — FFmpeg check/auto-install
- `util.py` — Video duration queries via FFmpeg

**`Src/Util/`** — Shared utilities
- `console.py` — Rich console singleton + optional debug logger (`SAVE_DEBUG` flag)
- `headers.py` — Random user-agent rotation (Chrome on Windows/Linux)

**`Src/Upload/`** — Version management
- `update.py` — Fetches GitHub releases, displays changelog
- `__version__.py` — Version constants

### Configuration

`data.json` stores the site domain (e.g. `{"domain": "report"}`). The full domain becomes `streamingcommunity.{domain}`. Video CDN is always `vixcloud.co`.

### Output Structure

```
videos/
├── MovieTitle.mp4
└── SeriesTitle/
    └── S01E01.mp4
```

Temp segments are written to `tmp/segments/` and cleaned up after merging.

### Selection Syntax

The CLI supports range/list selection for seasons and episodes:
- Single: `0`
- Range: `[1-5]`
- Discontinuous: `[1,3,5]`

### Quality Selection

Auto-selects highest available resolution: 1080p → 720p → 480p → 360p. Subtitles for all non-Italian non-auto languages are downloaded as `.vtt` files alongside the video.
