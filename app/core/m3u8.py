import logging
import os
import random
import sys
import shutil
import time
import threading
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import ffmpeg
from requests.adapters import HTTPAdapter
from m3u8 import M3U8 as M3U8_Lib
from tqdm.rich import tqdm
from tqdm import TqdmExperimentalWarning
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend

from app.config import get_settings
from app.core.ffmpeg_path import ffmpeg_file_arg, get_ffmpeg_exe
from app.core.headers import get_headers
from app.core.paths import windows_path_problem
from app.progress import DownloadCancelledError

warnings.filterwarnings("ignore", category=TqdmExperimentalWarning)
warnings.filterwarnings("ignore", category=UserWarning, module="cryptography")

logger = logging.getLogger(__name__)
DOWNLOAD_SUB = True
DOWNLOAD_DEFAULT_LANGUAGE = False

# Segment failures worth another attempt. A 4xx that is not one of these is a
# verdict rather than congestion: repeating it across a few thousand segments
# only delays the error without changing it.
RETRIABLE_SEGMENT_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_SEGMENT_BACKOFF = 8

# When to stop rather than grind on against a source that is serving nothing.
#
# Counted as an unbroken run: any segment that arrives resets it. Losing some
# segments is the ordinary case — sixteen workers provoke exactly the rate
# limiting that produces it — and repairing them is what the sequential second
# pass is for, so the breaker must only catch a source refusing outright.
#
# Two earlier shapes were wrong. A budget of 5% of the playlist aborted downloads
# that used to complete, because it tripped before ever reaching the pass that
# recovers them. A cumulative failure *ratio* then failed differently: refusals
# clustered at the start of a playlist are all counted before a single success
# is, so the ratio reads 100% on a source that is merely slow to warm up. A run
# broken by any success is immune to both.
#
# Set well above any refusal run seen in practice — a report of this behaviour
# involved 84 in a row on a source that was otherwise serving fine. The cost of
# it being too high is bounded (a dead source takes a few more minutes to reach
# the same error) while the cost of it being too low is a download that would
# have completed.
FAILURE_ABORT_STREAK = 200


# One writer per destination file.
#
# Two requests for the same title in different languages are two requests on
# purpose, but they resolve to the same path — the layout carries no language —
# so downloading both at once had them writing the same file concurrently and
# left it corrupt. The lock is held for the whole download, so the second waits
# and then overwrites cleanly with its own complete file.
#
# In memory, which is the same assumption the rest of the panel makes: job state
# lives in JobManager and the process is single by design.
_destination_locks: dict[str, threading.Lock] = {}
_destination_locks_guard = threading.Lock()


def _destination_lock(path: str) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(path))
    with _destination_locks_guard:
        lock = _destination_locks.get(key)
        if lock is None:
            lock = _destination_locks[key] = threading.Lock()
        return lock


def _segment_backoff(attempt: int) -> float:
    """Exponential, capped, and jittered.

    Sixteen worker threads back off in lockstep otherwise, and arrive back at a
    struggling CDN together — which is what produced the 503s in the first
    place.
    """
    return min(2 ** attempt, MAX_SEGMENT_BACKOFF) * (0.5 + random.random())


# Segment requests in flight across the whole process, not per download.
#
# ``max_segment_workers`` used to size each download's pool on its own, so the
# default queue of three concurrent downloads opened three pools of sixteen —
# 48 requests at once against the same CDN edge. That is what the reports of
# "one download in three fails" were: the edge shed the load with 503s, and the
# container's resolver started answering "Temporary failure in name resolution",
# which the retry loop cannot tell apart from a source that is simply gone.
#
# The setting keeps its meaning — how many segments to fetch at once — but now
# describes the panel rather than each download, so a queue of three is as
# polite to the source as a queue of one and merely takes longer to drain.
_segment_budget_guard = threading.Lock()
_segment_budget: threading.Semaphore | None = None
_segment_budget_size = 0


def segment_budget(size: int) -> threading.Semaphore:
    """The process-wide budget, resized when the setting changes.

    Resizing replaces the semaphore rather than adjusting it: a thread holding
    a permit releases the object it acquired, so the old one drains on its own
    and the two can briefly overlap. That window is bounded and self-correcting,
    and the alternative is tracking every outstanding permit to rebalance it.
    """
    global _segment_budget, _segment_budget_size
    size = max(1, int(size))
    with _segment_budget_guard:
        if _segment_budget is None or _segment_budget_size != size:
            _segment_budget = threading.Semaphore(size)
            _segment_budget_size = size
        return _segment_budget


def _new_session(pool_size: int) -> requests.Session:
    """One session per download, sized to the workers that will share it.

    Every segment used to go through a bare ``requests.get``, which opens a new
    TCP connection, a new TLS handshake and — the part that actually broke
    things — a new DNS lookup, for each of a playlist's thousands of segments.
    A session keeps the connection alive, so the host is resolved once per
    download instead of once per request.

    The pool is sized explicitly: urllib3's default of ten would leave sixteen
    workers discarding connections and reopening them, which is the same
    problem in miniature. Retries stay at zero because ``get_req_ts`` owns that
    policy, and urllib3's would silently multiply it.
    """
    session = requests.Session()
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


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
        self._failed_segments = set()
        self._saved_count = 0
        self._failure_streak = 0
        self._failed_lock = threading.Lock()
        self._abort = threading.Event()

        # Read once here rather than per segment: get_settings() opens and
        # parses data.json on every call, and this is on the path taken a few
        # thousand times per download.
        self._workers = max(1, int(get_settings().get("max_segment_workers", 16)))
        self._budget = segment_budget(self._workers)
        self._session = _new_session(self._workers)

        # One user-agent for the whole download, not one per request.
        # get_headers() builds a rotator and picks again on every call, so a
        # single film sent thousands of different agents down what is now one
        # connection — wasted work, and exactly the shape a CDN rate-limits.
        self._user_agent = get_headers()

    def parse_data(self, m3u8_content):
        m3u8_parser = M3U8_Parser()
        m3u8_parser.parse_data(m3u8_content)

        if self.key is not None and m3u8_parser.keys:
            self.decryption.parse_key(m3u8_parser.keys.get("iv"))

        self.segments = m3u8_parser.segments

    def _headers(self):
        h = {"user-agent": self._user_agent}
        if self.referer:
            h["referer"] = self.referer
        return h

    def _fetch_m3u8(self, url: str, what: str, max_retries: int = 3, retry_delay: int = 2):
        """Fetch one M3U8, riding out the errors this CDN hands out routinely.

        Shared by both fetches in ``get_info()``. They used to be written out
        separately and only the first one grew retries, so a transient 500 on
        the rendition playlist killed the whole download while the identical
        failure one request earlier would have been retried.

        Handles three things the bare call did not: a transient non-2xx, the
        ``?b=1`` quirk (some playlists are only served with it), and a
        connection that never completes.
        """
        current = url
        tried_with_b1 = "b=1" in url
        response = None

        for attempt in range(max_retries):
            last = attempt == max_retries - 1
            try:
                response = self._session.get(current, headers=self._headers(), timeout=15)
            except requests.RequestException as e:
                response = None
                logger.warning("%s M3U8 fetch failed (%s) — tentativo %d/%d",
                               what, e, attempt + 1, max_retries)
            else:
                if response.ok:
                    return response
                if response.status_code == 403 and not tried_with_b1:
                    # A different URL, so retry at once rather than waiting.
                    logger.warning("%s M3U8 fetch returned 403, retrying with ?b=1: %s", what, current)
                    current = current + ("&b=1" if "?" in current else "?b=1")
                    tried_with_b1 = True
                    continue
                logger.warning("%s M3U8 fetch returned HTTP %d — tentativo %d/%d",
                               what, response.status_code, attempt + 1, max_retries)
            if not last:
                time.sleep(retry_delay)

        status = response.status_code if response is not None else "nessuna risposta"
        raise RuntimeError(f"Failed to fetch {what} M3U8: HTTP {status}")

    def get_info(self):
        response = self._fetch_m3u8(self.url, "playlist")

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
            rendition_resp = self._fetch_m3u8(best_url, "rendition")
            rp = M3U8_Parser()
            rp.parse_data(rendition_resp.text)
            if self.key is not None and rp.keys:
                self.decryption.parse_key(rp.keys.get("iv"))
            self.segments = rp.segments
            logger.info("Rendition segments: %d, first=%s", len(self.segments), self.segments[0] if self.segments else "none")
        else:
            raise RuntimeError(f"M3U8 has no segments and no variant playlists. Content: {response.text[:200]!r}")

    def get_req_ts(self, ts_url):
        """Fetch one segment, retrying what is worth retrying.

        The loop ran three times but only a 429 ever reached the second
        iteration: every other status returned on the first attempt, so a CDN
        shedding load with 503s lost segments it would have served a moment
        later. An *exception* did retry, which meant the same outage was
        handled differently depending on whether the server answered badly or
        did not answer at all.

        Returns the bytes, or None once the segment is given up on.
        """
        for attempt in range(self.max_retry):
            if self._cancel and self._cancel.is_set():
                return None
            try:
                # The budget is held around the request alone. A thread waiting
                # out its backoff below must not occupy a slot the source is
                # willing to serve to someone else.
                with self._budget:
                    response = self._session.get(
                        ts_url, headers=self._headers(), timeout=(10, 30)
                    )
            except Exception as e:
                logger.warning("Segment exception (attempt %d/%d): %s",
                               attempt + 1, self.max_retry, e)
            else:
                if response.status_code == 200:
                    return response.content
                logger.warning("Segment HTTP %d (attempt %d/%d): ...%s", response.status_code,
                               attempt + 1, self.max_retry, ts_url[-60:])
                if response.status_code not in RETRIABLE_SEGMENT_STATUS:
                    return None
            if attempt < self.max_retry - 1:
                # Waiting is pointless once the source has been written off, but
                # the attempt already made still counts: bailing out before ever
                # asking is what made an early abort cascade through every
                # segment still in flight.
                if self._abort.is_set():
                    return None
                time.sleep(_segment_backoff(attempt))
        return None

    def _write_ts(self, index, content):
        ts_filename = os.path.join(self.temp_folder, f"{index}.ts")
        with open(ts_filename, "wb") as ts_file:
            if self.key and self.decryption.iv:
                ts_file.write(self.decryption.decrypt_ts(content))
            else:
                ts_file.write(content)

    def _record_success(self):
        """One segment obtained. Breaks the failure run: the source is serving."""
        with self._failed_lock:
            self._saved_count += 1
            self._failure_streak = 0

    def _record_failure(self, index, ts_url):
        """Note a segment given up on, and stop if nothing is getting through.

        Losing segments is not by itself a reason to abandon the download: the
        sequential second pass exists to recover them, and it arrives later and
        one request at a time, which is what a throttling source was asking for.
        The breaker is for the other case — a source refusing outright — where
        every remaining segment would cost its full attempts and waits to reach
        a conclusion already visible.
        """
        with self._failed_lock:
            self._failed_segments.add(index)
            self._failure_streak += 1
            streak = self._failure_streak
            lost = len(self._failed_segments)
        logger.warning("Segment %d failed after all retries: ...%s", index, ts_url[-60:])

        if streak < FAILURE_ABORT_STREAK or self._abort.is_set():
            return
        self._abort.set()
        logger.error("Aborting: %d segments in a row failed with none arriving between "
                     "them (%d lost of %d) — the source is refusing, not throttling",
                     streak, lost, len(self.segments))

    def save_ts(self, index, progress_counter, quit_event):
        if self._cancel and self._cancel.is_set():
            return
        if self._abort.is_set():
            return
        ts_url = self.segments[index]
        ts_filename = os.path.join(self.temp_folder, f"{index}.ts")

        downloaded_bytes = 0
        if not os.path.exists(ts_filename):
            ts_content = self.get_req_ts(ts_url)
            # Empty content counts as a failure: it would otherwise write a
            # zero-byte file, which then looks like a segment that is present.
            if not ts_content:
                self._record_failure(index, ts_url)
                return
            self._write_ts(index, ts_content)
            downloaded_bytes = len(ts_content)
        else:
            try:
                downloaded_bytes = os.path.getsize(ts_filename)
            except OSError:
                pass

        self._record_success()

        # Only a segment actually obtained counts. Counting attempts drove the
        # bar to 100% on a download that had saved barely half its segments,
        # and — worse — kept feeding the watchdog below, so a source failing
        # every single request still looked like it was making progress.
        progress_counter.update(1, bytes=downloaded_bytes)

    def download_ts(self):
        self._failed_segments = set()
        self._saved_count = 0
        self._failure_streak = 0
        self._abort.clear()
        bar_factory = self.progress_factory or (lambda **kw: tqdm(**kw))
        progress_counter = bar_factory(total=len(self.segments), unit="seg", desc="Downloading", phase=self.phase)
        self._bar = progress_counter

        quit_event = threading.Event()
        timeout_event = threading.Event()
        cancelled = False

        timer_thread = threading.Thread(
            target=self.timer, args=(progress_counter, quit_event, timeout_event.set)
        )
        timer_thread.start()

        try:
            with ThreadPoolExecutor(max_workers=self._workers) as executor:
                futures = []
                for index in range(len(self.segments)):
                    if timeout_event.is_set() or self._abort.is_set():
                        break
                    if self._cancel and self._cancel.is_set():
                        cancelled = True
                        break
                    futures.append(executor.submit(self.save_ts, index, progress_counter, quit_event))

                for future in as_completed(futures):
                    if timeout_event.is_set() or self._abort.is_set():
                        for f in futures:
                            f.cancel()
                        break
                    if self._cancel and self._cancel.is_set():
                        cancelled = True
                        for f in futures:
                            f.cancel()
                        break
                    try:
                        future.result()
                    except Exception as e:
                        logger.error(f"Segment error: {e}")

            # Stop the watchdog before the sequential pass. It measures stalls
            # against the parallel pass, and the retry below is deliberately
            # slow and one request at a time.
            quit_event.set()
            timer_thread.join()

            if cancelled:
                raise DownloadCancelledError("Download annullato dall'utente")

            # Both of these used to fall through to the join, which then wrote
            # an output file out of whatever had arrived.
            if self._abort.is_set():
                raise RuntimeError(
                    f"Download interrotto: la fonte ha rifiutato {len(self._failed_segments)} "
                    f"segmenti su {len(self.segments)} servendone quasi nessuno. "
                    f"Riprova più tardi."
                )
            if timeout_event.is_set():
                raise RuntimeError(
                    f"Download interrotto: nessun segmento scaricato per "
                    f"{self.progress_timeout}s. La fonte non risponde, riprova più tardi."
                )

            self._retry_failed_sequentially(progress_counter)
            self._require_every_segment()
        finally:
            # Closed here and not before the retry pass: a segment recovered
            # sequentially has to reach the bar, or a download that lost some
            # segments early stops short of its total and never emits the event
            # that says the phase is finished.
            progress_counter.close()
            quit_event.set()
            timer_thread.join()
            # Nothing after this fetches anything — join() only reads the files
            # already on disk — so the pooled connections are released here
            # rather than waiting on the garbage collector.
            self._session.close()

    def _retry_failed_sequentially(self, progress_counter):
        """Second pass over the segments the parallel pass gave up on.

        Worth a try even after their attempts are exhausted: this arrives later
        and one request at a time, which is what a source shedding load was
        asking for.
        """
        if not self._failed_segments:
            return
        logger.warning("Retrying %d failed segments sequentially...", len(self._failed_segments))
        still_failed = set()
        for index in sorted(self._failed_segments):
            if self._cancel and self._cancel.is_set():
                raise DownloadCancelledError("Download annullato dall'utente")
            time.sleep(0.5)
            ts_content = self.get_req_ts(self.segments[index])
            if ts_content:
                self._write_ts(index, ts_content)
                progress_counter.update(1, bytes=len(ts_content))
                logger.info("Segment %d recovered on retry", index)
            else:
                still_failed.add(index)
                logger.error("Segment %d permanently failed", index)
        self._failed_segments = still_failed

    def _missing_segments(self) -> list[int]:
        """Which segments are not on disk.

        Asks the filesystem rather than trusting ``_failed_segments``, which
        only knows about segments that were attempted. A run cut short by the
        watchdog never submits the rest, so the bookkeeping would call a
        download whole precisely when most of it is absent.
        """
        present = set()
        try:
            names = os.listdir(self.temp_folder)
        except OSError:
            names = []
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext == ".ts" and stem.isdigit():
                present.add(int(stem))
        return [i for i in range(len(self.segments)) if i not in present]

    def _require_every_segment(self):
        """Refuse to hand on an incomplete set of segments.

        A gap here is not a glitch to be logged: TS concatenated with segments
        missing produces a file that opens, plays, and is wrong — and lands in
        the library looking exactly like a good one. Failing is recoverable,
        because the download can be run again; a silently truncated film is not,
        because nobody knows to.
        """
        missing = self._missing_segments()
        if not missing:
            return
        preview = ", ".join(str(i) for i in missing[:10])
        if len(missing) > 10:
            preview += ", …"
        logger.error("Incomplete download: %d/%d segments missing (%s)",
                     len(missing), len(self.segments), preview)
        raise RuntimeError(
            f"Download incompleto: {len(missing)} segmenti su {len(self.segments)} "
            f"non scaricati. Il file non è stato creato per non salvarlo corrotto."
        )

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
        # Checked before anything is written, not after. This used to warn about
        # missing segments on the line *following* the concatenation, so the
        # warning described a file that had already been assembled from a
        # partial set — and the download still ended as "done".
        self._require_every_segment()

        # "_combined.ts" is excluded by name: it has no digits, so it used to
        # raise ValueError out of the sort key if a previous attempt left one
        # behind. Reachable now that a failed join can leave the folder in place.
        indexed = sorted(
            ((int(os.path.splitext(f)[0]), f) for f in os.listdir(self.temp_folder)
             if f.endswith(".ts") and os.path.splitext(f)[0].isdigit()),
        )
        ts_files = [name for _, name in indexed]

        # Byte-level concatenation: TS is a continuous stream format,
        # joining at byte level lets FFmpeg's TS demuxer handle PCR/PTS
        # continuity natively — avoids the timestamp drift of the concat demuxer.
        combined_ts = os.path.join(self.temp_folder, "_combined.ts")
        with open(combined_ts, "wb") as out:
            for ts_file in ts_files:
                with open(os.path.join(self.temp_folder, ts_file), "rb") as seg:
                    out.write(seg.read())

        logger.info("Joining %d / %d segments...", len(ts_files), len(self.segments))
        if self.emit_join_phase and hasattr(self, '_bar') and hasattr(self._bar, 'emit_status'):
            self._bar.emit_status("joining")
        os.makedirs(os.path.dirname(os.path.abspath(output_filename)), exist_ok=True)
        try:
            ffmpeg.input(combined_ts, fflags="+genpts", avoid_negative_ts="make_zero").output(
                ffmpeg_file_arg(output_filename),
                **{"c:v": "copy", "c:a": "aac", "b:a": "192k",
                   "af": "aresample=async=1000", "movflags": "+faststart"}
            ).overwrite_output().run(cmd=get_ffmpeg_exe(), capture_stdout=True, capture_stderr=True)
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

        # The remux is the merge, so the phase is announced here rather than in
        # the audio branch above. It used to be emitted only when separate audio
        # tracks had been downloaded, which left a film whose audio is already
        # muxed — remuxed all the same, to embed its subtitles — going straight
        # from "joining" to finished with the step never lighting up.
        if self.audio_paths or self.subtitle_track_urls or self.subtitle_languages:
            if bar and hasattr(bar, "emit_status"):
                bar.emit_status("merging")
            from app.core.format import remux_to_mkv, LANG_MAP
            video_stem = os.path.splitext(os.path.basename(self.video_path))[0]
            subtitle_tracks = []
            # Embed whatever subtitle vtts were actually downloaded to temp_dir by
            # download_m3u8 (keyed on subtitle_languages), NOT subtitle_track_urls —
            # the latter is collected via a 403-prone path and can be empty even when
            # the vtts downloaded fine, silently dropping subs from the output.
            seen_paths = set()
            for lang_code in self.subtitle_languages:
                lang_short = LANG_MAP.get(lang_code, lang_code)
                sub_path = os.path.join(self.temp_dir, f"{video_stem}.{lang_short}.vtt")
                if sub_path not in seen_paths and os.path.exists(sub_path):
                    seen_paths.add(sub_path)
                    subtitle_tracks.append({"path": sub_path, "language": lang_code})
            self.video_path = remux_to_mkv(
                self.video_path,
                audio_tracks=self.audio_paths,
                subtitle_tracks=subtitle_tracks,
            )
            for sub in subtitle_tracks:
                try:
                    os.remove(sub["path"])
                    logger.info("Cleaned up subtitle: %s", sub["path"])
                except OSError:
                    pass
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

        # remux_to_mkv may have changed the extension to .mkv — return the real path
        return self.video_path

    def join_audio(self):
        merged_path = self.video_path.replace(".mp4", "_merged.mp4")
        audio_path = os.path.join(self.temp_dir, "_audio_tmp.mp4")
        try:
            (
                ffmpeg
                .output(
                    ffmpeg.input(ffmpeg_file_arg(self.video_path)),
                    ffmpeg.input(audio_path),
                    ffmpeg_file_arg(merged_path),
                    vcodec="copy",
                    acodec="copy",
                    loglevel="quiet",
                )
                .global_args("-map", "0:v:0", "-map", "1:a:0", "-shortest", "-strict", "experimental")
                .overwrite_output()
                .run(cmd=get_ffmpeg_exe())
            )
            logger.info("Audio merge completed.")
        except ffmpeg.Error as e:
            raise RuntimeError(f"FFmpeg audio merge error: {e}")
        finally:
            if os.path.exists(audio_path):
                os.remove(audio_path)

        os.replace(merged_path, self.video_path)


def _fetch_text(url):
    response = requests.get(url, timeout=15)
    if response.ok:
        return response.text
    raise RuntimeError(f"Failed to fetch {url}: HTTP {response.status_code}")


def _fetch_text_with_b1_fallback(url):
    response = requests.get(url, timeout=15)
    if response.status_code == 403:
        logger.warning("M3U8 fetch returned 403, retrying with ?b=1: %s", url)
        b1_url = url + ("&b=1" if "?" in url else "?b=1")
        response = requests.get(b1_url, timeout=15)
    if response.ok:
        return response.text
    raise RuntimeError(f"Failed to fetch {url}: HTTP {response.status_code}")


def fetch_master_languages(m3u8_url: str, referer: str) -> dict:
    """Fetch a master M3U8 playlist and return available audio/subtitle language codes."""
    text = _fetch_text_with_b1_fallback(m3u8_url)
    parser = M3U8_Parser()
    parser.parse_data(text)
    return parser.available_languages()


def download_m3u8(**kwargs):
    """Download one title, with nobody else writing the same file meanwhile.

    Two requests for the same title in different languages are deliberately kept
    apart — the content key includes the chosen tracks — but they resolve to the
    same path, because the library layout carries no language. Approving both
    therefore had two downloads writing one file at the same time, and the
    result was a corrupt film that still looked like a film.

    Serialising them means the second waits and then writes its own complete
    file over the first. Combined with resolving every request to the union of
    what has been asked for, the file left behind is the one that satisfies
    everybody rather than whoever finished last.

    Every caller passes keyword arguments, which is what lets this wrap the real
    body without restating its signature.
    """
    destination = kwargs.get("output_filename") or os.path.join("videos", "output.mp4")
    lock = _destination_lock(destination)
    if not lock.acquire(blocking=False):
        logger.info("Waiting for another download to finish writing %s", destination)
        lock.acquire()
    try:
        return _download_m3u8(**kwargs)
    finally:
        lock.release()


def _download_m3u8(
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
    # Checked before a single segment is fetched. A library configured with a
    # Windows path on a Linux host used to get all the way here, create a
    # directory literally named "N:\Jellyfin\Anime" beside the app, download
    # the whole episode, and only then fail in FFmpeg with "Protocol not found"
    # — an error that says nothing about the actual mistake.
    problem = windows_path_problem(output_filename)
    if problem:
        raise RuntimeError(f"Percorso di destinazione non valido. {problem}")

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
            created = download_subtitle_tracks(parse_class_m3u8, subtitle_languages, video_dir, video_stem, temp_dir=temp_dir)
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
            created = download_subtitle_tracks(parse_master, subtitle_languages, video_dir, video_stem, temp_dir=temp_dir)
            created_subtitle_files.extend(t["path"] for t in created)
        except Exception as e:
            logger.warning("Could not parse subtitles from master playlist: %s", e)

    if m3u8_subtitle is not None:
        parse_sub = M3U8_Parser()
        content_sub = m3u8_subtitle if "#EXTM3U" in m3u8_subtitle else _fetch_text(m3u8_subtitle)
        parse_sub.parse_data(content_sub)
        if DOWNLOAD_SUB and subtitle_languages:
            from app.core.format import download_subtitle_tracks
            created = download_subtitle_tracks(parse_sub, subtitle_languages, video_dir, video_stem, temp_dir=temp_dir)
            created_subtitle_files.extend(t["path"] for t in created)

    os.makedirs(os.path.dirname(output_filename) or ".", exist_ok=True)

    try:
        return M3U8_Downloader(
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
