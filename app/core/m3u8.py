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

from app.core.headers import get_headers
from app.progress import DownloadCancelledError

warnings.filterwarnings("ignore", category=TqdmExperimentalWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="cryptography")

logger = logging.getLogger(__name__)

MAX_WORKER = 16
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
                self.video_playlist.append({"uri": playlist.uri})
                self.stream_infos = {
                    "bandwidth": playlist.stream_info.bandwidth,
                    "codecs": playlist.stream_info.codecs,
                    "resolution": playlist.stream_info.resolution,
                }

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

    def get_best_quality(self):
        if self.video_playlist:
            return self.video_playlist[0].get("uri")
        logger.warning("No video playlist found")
        return None

    def download_subtitle(self, subtitle_dir: str):
        if not self.subtitle_playlist:
            logger.info("No subtitles found")
            return

        os.makedirs(subtitle_dir, exist_ok=True)
        for sub_info in self.subtitle_playlist:
            name_language = sub_info.get("language")
            if name_language in ["auto", "ita"]:
                continue
            logger.info(f"Downloading subtitle: {name_language}")
            req_sub_content = requests.get(sub_info.get("uri"))
            sub_parse = M3U8_Parser()
            sub_parse.parse_data(req_sub_content.text)
            if sub_parse.subtitle:
                open(os.path.join(subtitle_dir, name_language + ".vtt"), "wb").write(
                    requests.get(sub_parse.subtitle[0]).content
                )

    def get_track_audio(self, language_name):
        if self.audio_ts:
            if language_name is not None:
                for obj_audio in self.audio_ts:
                    if obj_audio.get("name") == language_name:
                        return obj_audio.get("uri")
        return None


class M3U8_Segments:
    def __init__(self, url, key=None, temp_dir=None, progress_factory=None, referer=None, cancel_event=None):
        self.url = url
        self.key = key
        self.referer = referer
        self._cancel = cancel_event

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
        response = requests.get(self.url, headers=self._headers())
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
            best_url = parser.video_playlist[0].get("uri")
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

    def save_ts(self, index, progress_counter, quit_event):
        if self._cancel and self._cancel.is_set():
            return
        ts_url = self.segments[index]
        ts_filename = os.path.join(self.temp_folder, f"{index}.ts")

        if not os.path.exists(ts_filename):
            ts_content = self.get_req_ts(ts_url)
            if ts_content is not None:
                with open(ts_filename, "wb") as ts_file:
                    if self.key and self.decryption.iv:
                        ts_file.write(self.decryption.decrypt_ts(ts_content))
                    else:
                        ts_file.write(ts_content)

        progress_counter.update(1)

    def download_ts(self):
        bar_factory = self.progress_factory or (lambda **kw: tqdm(**kw))
        progress_counter = bar_factory(total=len(self.segments), unit="seg", desc="Downloading")
        self._bar = progress_counter

        quit_event = threading.Event()
        timeout_occurred = False
        cancelled = False

        timer_thread = threading.Thread(
            target=self.timer, args=(progress_counter, quit_event, lambda: timeout_occurred)
        )
        timer_thread.start()

        try:
            with ThreadPoolExecutor(max_workers=MAX_WORKER) as executor:
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

        logger.info("Joining %d / %d segments...", len(ts_files), len(self.segments))
        if hasattr(self, '_bar') and hasattr(self._bar, 'emit_status'):
            self._bar.emit_status("joining")
        os.makedirs(os.path.dirname(os.path.abspath(output_filename)), exist_ok=True)
        try:
            ffmpeg.input(combined_ts).output(
                output_filename, **{"c:v": "copy", "c:a": "aac", "b:a": "192k"}
            ).run(capture_stdout=True, capture_stderr=True)
        except ffmpeg.Error as e:
            stderr = e.stderr.decode(errors="replace") if e.stderr else "(no stderr)"
            logger.error("FFmpeg join stderr: %s", stderr)
            raise RuntimeError(f"FFmpeg join error: {stderr[:400]}")

        logger.info("Cleaning temp segments...")
        shutil.rmtree(self.temp_folder, ignore_errors=True)


class M3U8_Downloader:
    def __init__(self, m3u8_url, m3u8_audio=None, key=None, output_filename="output.mp4",
                 temp_dir=None, progress_factory=None, referer=None, cancel_event=None):
        self.m3u8_url = m3u8_url
        self.m3u8_audio = m3u8_audio
        self.key = key
        self.video_path = output_filename
        self.temp_dir = temp_dir or os.path.join("tmp", "segments")
        self.progress_factory = progress_factory
        self.referer = referer
        self.cancel_event = cancel_event

        self.audio_path = os.path.join(self.temp_dir, "_audio_tmp.mp4")

    def start(self):
        video_temp = os.path.join(self.temp_dir, "video")
        video_m3u8 = M3U8_Segments(self.m3u8_url, self.key,
                                    temp_dir=video_temp,
                                    progress_factory=self.progress_factory,
                                    referer=self.referer,
                                    cancel_event=self.cancel_event)
        logger.info("Downloading video segments...")
        video_m3u8.get_info()
        video_m3u8.download_ts()
        video_m3u8.join(self.video_path)

        if self.m3u8_audio is not None:
            audio_temp = os.path.join(self.temp_dir, "audio")
            audio_m3u8 = M3U8_Segments(self.m3u8_audio, self.key,
                                        temp_dir=audio_temp,
                                        referer=self.referer)
            logger.info("Downloading audio segments...")
            audio_m3u8.get_info()
            audio_m3u8.download_ts()
            audio_m3u8.join(self.audio_path)
            self.join_audio()

    def join_audio(self):
        merged_path = self.video_path.replace(".mp4", "_merged.mp4")
        try:
            (
                ffmpeg
                .output(
                    ffmpeg.input(self.video_path),
                    ffmpeg.input(self.audio_path),
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
            if os.path.exists(self.audio_path):
                os.remove(self.audio_path)

        os.replace(merged_path, self.video_path)


def _fetch_text(url):
    response = requests.get(url)
    if response.ok:
        return response.text
    raise RuntimeError(f"Failed to fetch {url}: HTTP {response.status_code}")


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
):
    key = bytes.fromhex(key) if key is not None else None

    subtitle_dir = os.path.join(os.path.dirname(output_filename), "subtitle")

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

        if DOWNLOAD_SUB:
            parse_class_m3u8.download_subtitle(subtitle_dir)

    if m3u8_subtitle is not None:
        parse_sub = M3U8_Parser()
        content_sub = m3u8_subtitle if "#EXTM3U" in m3u8_subtitle else _fetch_text(m3u8_subtitle)
        parse_sub.parse_data(content_sub)
        if DOWNLOAD_SUB:
            parse_sub.download_subtitle(subtitle_dir)

    os.makedirs(os.path.dirname(output_filename) or ".", exist_ok=True)

    M3U8_Downloader(
        m3u8_index,
        m3u8_audio,
        key=key,
        output_filename=output_filename,
        temp_dir=temp_dir,
        progress_factory=progress_factory,
        referer=referer,
        cancel_event=cancel_event,
    ).start()
