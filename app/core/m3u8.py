import logging
import os
import sys
import shutil
import time
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import ffmpeg
from m3u8 import M3U8 as M3U8_Lib
from tqdm.rich import tqdm
from tqdm import TqdmExperimentalWarning
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from app.config import get_settings
from app.core.headers import get_headers
from app.progress import DownloadCancelledError

warnings.filterwarnings("ignore", category=TqdmExperimentalWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="cryptography")

logger = logging.getLogger(__name__)
DOWNLOAD_SUB = True
DOWNLOAD_DEFAULT_LANGUAGE = False


class Decryption:
    def __init__(self, key):
        self.iv = None
        self.key = key

    def parse_key(self, raw_iv):
        self.iv = bytes.fromhex(raw_iv.replace("0x", ""))

    def decrypt_ts(self, encrypted_data):
        cipher = Cipher(algorithms.AES(self.key), modes.CBC(self.iv), backend=default_backend())
        decryptor = cipher.decryptor()
        return decryptor.update(encrypted_data) + decryptor.finalize()


class M3U8_Parser:
    def __init__(self):
        self.segments = []
        self.video_playlist = []
        self.keys = []
        self.subtitle_playlist = []
        self.subtitle = []
        self.audio_ts = []

    def parse_data(self, m3u8_content):
        try:
            m3u8_obj = M3U8_Lib(m3u8_content)

            for playlist in m3u8_obj.playlists:
                self.video_playlist.append({
                    "uri": playlist.uri,
                    "bandwidth": playlist.stream_info.bandwidth or 0,
                    "resolution": playlist.stream_info.resolution,
                })

            for key in m3u8_obj.keys:
                if key is not None:
                    self.keys = {
                        "method": key.method,
                        "uri": key.uri,
                        "iv": key.iv,
                    }

            for media in m3u8_obj.media:
                if media.type == "SUBTITLES":
                    self.subtitle_playlist.append({
                        "type": media.type,
                        "name": media.name,
                        "default": media.default,
                        "language": media.language,
                        "uri": media.uri,
                    })
                else:
                    self.audio_ts.append({
                        "type": media.type,
                        "name": media.name,
                        "default": media.default,
                        "language": media.language,
                        "uri": media.uri,
                    })

            for segment in m3u8_obj.segments:
                if "vtt" not in segment.uri:
                    self.segments.append(segment.uri)
                else:
                    self.subtitle.append(segment.uri)

        except Exception as e:
            logger.error(f"Error parsing M3U8 content: {e}")
            raise

    def get_best_quality(self):
        if self.video_playlist:
            best = max(self.video_playlist, key=lambda p: p.get("bandwidth") or 0)
            logger.info("Selected quality: %s bandwidth=%s", best.get("resolution"), best.get("bandwidth"))
            return best.get("uri")
        logger.warning("No video playlist found")
        return None

    def available_languages(self) -> dict:
        """Return available audio and subtitle language codes from the master playlist."""
        audio = [t.get("language") for t in self.audio_ts if t.get("language")]
        subtitles = [s.get("language") for s in self.subtitle_playlist if s.get("language") and s.get("language") != "auto"]
        return {"audio": audio, "subtitles": subtitles}

    # ISO 639-2 (3-letter) → ISO 639-1 (2-letter) for Jellyfin-compatible filenames
    _LANG_MAP = {
        "ita": "it", "eng": "en", "fra": "fr", "spa": "es", "deu": "de",
        "por": "pt", "jpn": "ja", "zho": "zh", "ara": "ar", "rus": "ru",
        "kor": "ko", "nld": "nl", "pol": "pl", "swe": "sv", "nor": "no",
        "dan": "da", "fin": "fi", "tur": "tr", "ell": "el", "ces": "cs",
    }

    def download_subtitle(self, video_dir: str, video_stem: str):
        """Save subtitles alongside the video as {video_stem}.{lang}.vtt (Jellyfin convention)."""
        if not self.subtitle_playlist:
            logger.info("No subtitles found")
            return

        os.makedirs(video_dir, exist_ok=True)
        for sub_info in self.subtitle_playlist:
            lang_code = sub_info.get("language", "")
            if lang_code not in ["ita", "eng"]:
                continue
            lang_short = self._LANG_MAP.get(lang_code, lang_code)
            out_path = os.path.join(video_dir, f"{video_stem}.{lang_short}.vtt")
            logger.info("Downloading subtitle: %s → %s", lang_code, out_path)
            req_sub_content = requests.get(sub_info.get("uri"))
            sub_parse = M3U8_Parser()
            sub_parse.parse_data(req_sub_content.text)
            if sub_parse.subtitle:
                open(out_path, "wb").write(requests.get(sub_parse.subtitle[0]).content)

    def get_track_audio(self, language_name):
        if self.audio_ts:
            if language_name is not None:
                for obj_audio in self.audio_ts:
                    if obj_audio.get("name") == language_name:
                        return obj_audio.get("uri")
        return None


class M3U8_Segments:
    def __init__(self, url, key=None, temp_dir=None, progress_factory=None, referer=None, cancel_event=None, phase=None, emit_join_phase=True):
        self.url = url
        self.key = key
        self.referer = referer
        self._cancel = cancel_event
        self.phase = phase
        self.emit_join_phase = emit_join_phase

        if key is not None:
            self.decryption = Decryption(key)

        self.temp_folder = temp_dir if temp_dir is not None else os.path.join("tmp", "segments")
        os.makedirs(self.temp_folder, exist_ok=True)

        self.progress_factory = progress_factory
        self.progress_timeout = 30
        self.max_retry = 3

    def parse_data(self, m3u8_content):
        m3u8_parser = M3U8_Parser()
        m3u8_parser.parse_data(m3u8_content)

        if self.key is not None and m3u8_parser.keys:
            self.decryption.parse_key(m3u8_parser.keys.get("iv"))

        self.segments = m3u8_parser.segments

    def _headers(self):
        h = {"user-agent": get_headers()}
        if self.referer:
            h["referer"] = self.referer
        return h

    def get_info(self):
        # Retry logic for transient failures like 403
        max_retries = 3
        retry_delay = 2  # seconds
        current_url = self.url
        tried_with_b1 = False
        
        for attempt in range(max_retries):
            response = requests.get(current_url, headers=self._headers())
            if response.ok:
                break
            # On 403, retry with &b1 parameter (some TV episodes require it)
            if response.status_code == 403 and attempt < max_retries - 1:
                if not tried_with_b1:
                    logger.warning(f"M3U8 fetch returned HTTP 403, retrying with &b=1... (attempt {attempt+1}/{max_retries})")
                    current_url = self.url + ("&b=1" if "?" in self.url else "?b=1")
                    tried_with_b1 = True
                    time.sleep(retry_delay)
                    continue
                else:
                    logger.warning(f"M3U8 fetch returned HTTP 403, retrying in {retry_delay}s... (attempt {attempt+1}/{max_retries})")
                    time.sleep(retry_delay)
                    continue
            # Other errors: fail immediately
            if not response.ok:
                raise RuntimeError(f"Failed to fetch M3U8: HTTP {response.status_code}")
        
        if not response.ok:
            raise RuntimeError(f"Failed to fetch M3U8: HTTP {response.status_code}")

        parser = M3U8_Parser()
        parser.parse_data(response.text)

        if self.key is not None and parser.keys:
            self.decryption.parse_key(parser.keys.get("iv"))

        if parser.segments:
            # Direct segment playlist
            self.segments = parser.segments
            logger.info("Direct segment playlist: %d segments, first=%s", len(self.segments), self.segments[0] if self.segments else "none")
        elif parser.video_playlist:
            # Master playlist — resolve the best quality rendition
            best_url = parser.get_best_quality()
            logger.info("Master playlist detected (%d variants), fetching best rendition: %s", len(parser.video_playlist), best_url)
            rendition_resp = requests.get(best_url, headers=self._headers())
            if not rendition_resp.ok:
                raise RuntimeError(f"Failed to fetch rendition M3U8: HTTP {rendition_resp.status_code}")
            rp = M3U8_Parser()
            rp.parse_data(rendition_resp.text)
            if self.key is not None and rp.keys:
                self.decryption.parse_key(rp.keys.get("iv"))
            self.segments = rp.segments
            logger.info("Rendition segments: %d, first=%s", len(self.segments), self.segments[0] if self.segments else "none")
        else:
            raise RuntimeError(f"M3U8 has no segments and no variant playlists. Content: {response.text[:200]!r}")

    def get_req_ts(self, ts_url):
        for attempt in range(3):
            try:
                response = requests.get(ts_url, headers={"user-agent": get_headers()}, timeout=10)
                if response.status_code == 200:
                    return response.content
                logger.warning("Segment HTTP %d (attempt %d): ...%s", response.status_code, attempt + 1, ts_url[-60:])
                if response.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                return None
            except Exception as e:
                logger.warning("Segment exception (attempt %d): %s", attempt + 1, e)
                time.sleep(1)
        return None

    def _write_ts(self, index, content):
        ts_filename = os.path.join(self.temp_folder, f"{index}.ts")
        with open(ts_filename, "wb") as ts_file:
            if self.key and self.decryption.iv:
                ts_file.write(self.decryption.decrypt_ts(content))
            else:
                ts_file.write(content)

    def save_ts(self, index, progress_counter, quit_event):
        if self._cancel and self._cancel.is_set():
            return
        ts_url = self.segments[index]
        ts_filename = os.path.join(self.temp_folder, f"{index}.ts")

        if not os.path.exists(ts_filename):
            ts_content = self.get_req_ts(ts_url)
            if ts_content is not None:
                self._write_ts(index, ts_content)
            else:
                self._failed_segments.add(index)
                logger.warning("Segment %d failed after all retries: ...%s", index, ts_url[-60:])

        progress_counter.update(1)

    def download_ts(self):
        self._failed_segments = set()
        bar_factory = self.progress_factory or (lambda **kw: tqdm(**kw))
        progress_counter = bar_factory(total=len(self.segments), unit="seg", desc="Downloading", phase=self.phase)
        self._bar = progress_counter

        quit_event = threading.Event()
        timeout_occurred = False
        cancelled = False

        timer_thread = threading.Thread(
            target=self.timer, args=(progress_counter, quit_event, lambda: timeout_occurred)
        )
        timer_thread.start()

        try:
            with ThreadPoolExecutor(max_workers=get_settings().get("max_segment_workers", 16)) as executor:
                futures = []
                for index in range(len(self.segments)):
                    if timeout_occurred:
                        break
                    if self._cancel and self._cancel.is_set():
                        cancelled = True
                        break
                    futures.append(executor.submit(self.save_ts, index, progress_counter, quit_event))

                for future in as_completed(futures):
                    if self._cancel and self._cancel.is_set():
                        cancelled = True
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Segment error: {e}")
        finally:
            progress_counter.close()
            quit_event.set()
            timer_thread.join()

        if cancelled:
            raise DownloadCancelledError("Download annullato dall'utente")

        # Second pass: retry failed segments sequentially to avoid gaps in the TS stream
        if self._failed_segments:
            logger.warning("Retrying %d failed segments sequentially...", len(self._failed_segments))
            still_failed = set()
            for index in sorted(self._failed_segments):
                if self._cancel and self._cancel.is_set():
                    raise DownloadCancelledError("Download annullato dall'utente")
                time.sleep(0.5)
                ts_content = self.get_req_ts(self.segments[index])
                if ts_content is not None:
                    self._write_ts(index, ts_content)
                    logger.info("Segment %d recovered on retry", index)
                else:
                    still_failed.add(index)
                    logger.error("Segment %d permanently failed", index)
            self._failed_segments = still_failed

    def timer(self, progress_counter, quit_event, timeout_checker):
        start_time = time.time()
        last_count = 0

        while not quit_event.is_set():
            current_count = progress_counter.n
            if current_count != last_count:
                start_time = time.time()
                last_count = current_count

            if time.time() - start_time > self.progress_timeout:
                logger.warning(f"No progress for {self.progress_timeout}s, aborting download")
                timeout_checker()
                quit_event.set()
                break

            time.sleep(1)

        progress_counter.refresh()

    def join(self, output_filename):
        if self._cancel and self._cancel.is_set():
            raise DownloadCancelledError("Download annullato dall'utente")
        ts_files = sorted(
            [f for f in os.listdir(self.temp_folder) if f.endswith(".ts")],
            key=lambda f: int("".join(filter(str.isdigit, f))),
        )

        # Byte-level concatenation: TS is a continuous stream format,
        # joining at byte level lets FFmpeg's TS demuxer handle PCR/PTS
        # continuity natively — avoids the timestamp drift of the concat demuxer.
        combined_ts = os.path.join(self.temp_folder, "_combined.ts")
        with open(combined_ts, "wb") as out:
            for ts_file in ts_files:
                with open(os.path.join(self.temp_folder, ts_file), "rb") as seg:
                    out.write(seg.read())

        still_failed = getattr(self, "_failed_segments", set())
        if still_failed:
            logger.warning("%d segments permanently missing from output: %s", len(still_failed), sorted(still_failed))

        logger.info("Joining %d / %d segments...", len(ts_files), len(self.segments))
        if self.emit_join_phase and hasattr(self, '_bar') and hasattr(self._bar, 'emit_status'):
            self._bar.emit_status("joining")
        os.makedirs(os.path.dirname(os.path.abspath(output_filename)), exist_ok=True)
        try:
            ffmpeg.input(combined_ts).output(
                output_filename, **{"c:v": "copy", "c:a": "aac", "b:a": "192k"}
            ).run(capture_stdout=True, capture_stderr=True)
        except ffmpeg.Error as e:
            stderr = e.stderr.decode(errors="replace") if e.stderr else "(no stderr)"
            if len(stderr) > 500:
                stderr = f"...{stderr[-300:]}"
            logger.error("FFmpeg join stderr: %s", stderr)
            raise RuntimeError(f"FFmpeg join error: {stderr}")

        logger.info("Cleaning temp segments...")
        shutil.rmtree(self.temp_folder, ignore_errors=True)


class M3U8_Downloader:
    def __init__(self, m3u8_url, m3u8_audio=None, key=None, output_filename="output.mp4",
                 temp_dir=None, progress_factory=None, referer=None, cancel_event=None,
                 audio_languages: list[str] = None,
                 subtitle_languages: list[str] = None,
                 audio_track_urls: list[dict] = None,
                 subtitle_track_urls: list[dict] = None):
        self.m3u8_url = m3u8_url
        self.m3u8_audio = m3u8_audio
        self.key = key
        self.video_path = output_filename
        self.temp_dir = temp_dir or os.path.join("tmp", "segments")
        self.progress_factory = progress_factory
        self.referer = referer
        self.cancel_event = cancel_event
        self.audio_languages = audio_languages or ["ita"]
        self.subtitle_languages = subtitle_languages or []
        self.audio_track_urls = audio_track_urls or []
        self.subtitle_track_urls = subtitle_track_urls or []

        self.audio_paths: list[str] = []

    def start(self):
        video_temp = os.path.join(self.temp_dir, "video")
        video_m3u8 = M3U8_Segments(self.m3u8_url, self.key,
                                    temp_dir=video_temp,
                                    progress_factory=self.progress_factory,
                                    referer=self.referer,
                                    cancel_event=self.cancel_event,
                                    phase="video")
        logger.info("Downloading video segments...")
        video_m3u8.get_info()
        video_m3u8.download_ts()
        bar = getattr(video_m3u8, "_bar", None)
        video_m3u8.join(self.video_path)

        if self.audio_track_urls:
            for i, track in enumerate(self.audio_track_urls):
                lang = track.get("language", "und")
                phase_label = f"audio_{lang}"
                if bar and hasattr(bar, "emit_status"):
                    bar.emit_status(phase_label)
                audio_temp = os.path.join(self.temp_dir, f"audio_{i}")
                audio_m3u8 = M3U8_Segments(track["url"], self.key,
                                            temp_dir=audio_temp,
                                            progress_factory=self.progress_factory,
                                            referer=self.referer,
                                            cancel_event=self.cancel_event,
                                            phase=phase_label,
                                            emit_join_phase=False)
                logger.info("Downloading audio track %d (%s)...", i + 1, lang)
                audio_m3u8.get_info()
                audio_m3u8.download_ts()
                audio_path = os.path.join(self.temp_dir, f"_audio_{i}.mp4")
                audio_m3u8.join(audio_path)
                self.audio_paths.append({"path": audio_path, "language": lang})

            if bar and hasattr(bar, "emit_status"):
                bar.emit_status("merging")

        if self.audio_paths:
            from app.core.format import remux_to_mkv
            self.video_path = remux_to_mkv(
                self.video_path,
                audio_tracks=self.audio_paths,
                subtitle_tracks=None,
            )
        elif self.m3u8_audio is not None:
            if bar and hasattr(bar, "emit_status"):
                bar.emit_status("audio")

            audio_temp = os.path.join(self.temp_dir, "audio")
            audio_m3u8 = M3U8_Segments(self.m3u8_audio, self.key,
                                        temp_dir=audio_temp,
                                        progress_factory=self.progress_factory,
                                        referer=self.referer,
                                        cancel_event=self.cancel_event,
                                        phase="audio",
                                        emit_join_phase=False)
            logger.info("Downloading audio track...")
            audio_m3u8.get_info()
            audio_m3u8.download_ts()
            audio_path = os.path.join(self.temp_dir, "_audio_tmp.mp4")
            audio_m3u8.join(audio_path)
            if bar and hasattr(bar, "emit_status"):
                bar.emit_status("merging")
            self.join_audio()

    def join_audio(self):
        merged_path = self.video_path.replace(".mp4", "_merged.mp4")
        audio_path = os.path.join(self.temp_dir, "_audio_tmp.mp4")
        try:
            (
                ffmpeg
                .output(
                    ffmpeg.input(self.video_path),
                    ffmpeg.input(audio_path),
                    merged_path,
                    vcodec="copy",
                    acodec="copy",
                    loglevel="quiet",
                )
                .global_args("-map", "0:v:0", "-map", "1:a:0", "-shortest", "-strict", "experimental")
                .run()
            )
            logger.info("Audio merge completed.")
        except ffmpeg.Error as e:
            raise RuntimeError(f"FFmpeg audio merge error: {e}")
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)

        os.replace(merged_path, self.video_path)


def _fetch_text(url):
    response = requests.get(url)
    if response.ok:
        return response.text
    raise RuntimeError(f"Failed to fetch {url}: HTTP {response.status_code}")


def _fetch_text_with_b1_fallback(url):
    response = requests.get(url)
    if response.status_code == 403:
        logger.warning("M3U8 fetch returned 403, retrying with ?b=1: %s", url)
        b1_url = url + ("&b=1" if "?" in url else "?b=1")
        response = requests.get(b1_url)
    if response.ok:
        return response.text
    raise RuntimeError(f"Failed to fetch {url}: HTTP {response.status_code}")


def fetch_master_languages(m3u8_url: str, referer: str) -> dict:
    """Fetch a master M3U8 playlist and return available audio/subtitle language codes."""
    req = requests.get(m3u8_url, headers={"user-agent": get_headers(), "referer": referer}, timeout=10)
    if not req.ok:
        raise RuntimeError(f"Master M3U8 returned HTTP {req.status_code}")
    parser = M3U8_Parser()
    parser.parse_data(req.text)
    return parser.available_languages()


def download_m3u8(
    m3u8_playlist=None,
    m3u8_index=None,
    m3u8_audio=None,
    m3u8_subtitle=None,
    key=None,
    output_filename=os.path.join("videos", "output.mp4"),
    temp_dir=None,
    progress_factory=None,
    referer=None,
    cancel_event=None,
    audio_languages: list[str] = None,
    subtitle_languages: list[str] = None,
    audio_track_urls: list[dict] = None,
    subtitle_track_urls: list[dict] = None,
):
    key = bytes.fromhex(key) if key is not None else None

    # Jellyfin convention: subtitles live next to the video as {stem}.{lang}.vtt
    video_dir = os.path.dirname(output_filename) or "."
    video_stem = os.path.splitext(os.path.basename(output_filename))[0]

    audio_languages = audio_languages or ["ita"]
    subtitle_languages = subtitle_languages or []
    audio_track_urls = audio_track_urls or []
    subtitle_track_urls = subtitle_track_urls or []

    # Track subtitle files created before the main download so they can be cleaned
    # up if the download is cancelled or fails mid-way.
    created_subtitle_files: list[str] = []

    if m3u8_playlist is not None:
        parse_class_m3u8 = M3U8_Parser()
        content = m3u8_playlist if "#EXTM3U" in m3u8_playlist else _fetch_text(m3u8_playlist)
        parse_class_m3u8.parse_data(content)

        if DOWNLOAD_DEFAULT_LANGUAGE:
            m3u8_audio = parse_class_m3u8.get_track_audio("Italian")

        if m3u8_index is None:
            m3u8_index = parse_class_m3u8.get_best_quality()
            if not m3u8_index or "https" not in m3u8_index:
                raise RuntimeError("Cannot find a valid M3U8 index URL")

        langs = parse_class_m3u8.available_languages()
        logger.info("Available languages — audio: %s | subtitles: %s", langs["audio"], langs["subtitles"])

        if DOWNLOAD_SUB and subtitle_languages:
            from app.core.format import download_subtitle_tracks
            created = download_subtitle_tracks(parse_class_m3u8, subtitle_languages, video_dir, video_stem)
            created_subtitle_files.extend(t["path"] for t in created)

    elif m3u8_index is not None and DOWNLOAD_SUB and subtitle_languages:
        # m3u8_index is a master playlist URL — parse it to extract subtitles
        try:
            from app.core.format import download_subtitle_tracks
            master_content = _fetch_text_with_b1_fallback(m3u8_index)
            parse_master = M3U8_Parser()
            parse_master.parse_data(master_content)
            langs = parse_master.available_languages()
            logger.info("Available languages — audio: %s | subtitles: %s", langs["audio"], langs["subtitles"])
            created = download_subtitle_tracks(parse_master, subtitle_languages, video_dir, video_stem)
            created_subtitle_files.extend(t["path"] for t in created)
        except Exception as e:
            logger.warning("Could not parse subtitles from master playlist: %s", e)

    if m3u8_subtitle is not None:
        parse_sub = M3U8_Parser()
        content_sub = m3u8_subtitle if "#EXTM3U" in m3u8_subtitle else _fetch_text(m3u8_subtitle)
        parse_sub.parse_data(content_sub)
        if DOWNLOAD_SUB and subtitle_languages:
            from app.core.format import download_subtitle_tracks
            created = download_subtitle_tracks(parse_sub, subtitle_languages, video_dir, video_stem)
            created_subtitle_files.extend(t["path"] for t in created)

    os.makedirs(os.path.dirname(output_filename) or ".", exist_ok=True)

    try:
        M3U8_Downloader(
            m3u8_index,
            m3u8_audio,
            key=key,
            output_filename=output_filename,
            temp_dir=temp_dir,
            progress_factory=progress_factory,
            referer=referer,
            cancel_event=cancel_event,
            audio_languages=audio_languages,
            subtitle_languages=subtitle_languages,
            audio_track_urls=audio_track_urls,
            subtitle_track_urls=subtitle_track_urls,
        ).start()
    except (DownloadCancelledError, Exception) as exc:
        # Remove subtitle files already written to the output dir
        for path in created_subtitle_files:
            try:
                os.remove(path)
                logger.info("Removed partial subtitle: %s", path)
            except OSError:
                pass
        # Remove partial video / remuxed MKV if they exist
        stem = os.path.splitext(output_filename)[0]
        for candidate in (output_filename, stem + ".mkv"):
            try:
                os.remove(candidate)
                logger.info("Removed partial output: %s", candidate)
            except OSError:
                pass
        # Remove the output directory if it's now empty
        try:
            if video_dir and video_dir != "." and not os.listdir(video_dir):
                os.rmdir(video_dir)
        except OSError:
            pass
        raise
