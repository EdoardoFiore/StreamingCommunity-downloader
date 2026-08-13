"""Notifications for downloads that skipped the request queue.

The request system reports on requests; this reports on jobs nobody requested —
the direct-download path. It also collapses a season or a series asked for in one
go into a single message: twenty-four "episode ready" pings for one season is
noise, not news.

Lives outside ``app/requests/`` on purpose. A direct download is by definition
the path that bypassed the queue, and putting the aggregator there would tempt
``notify.py`` into importing ``jobs``.
"""

import logging
import re
import threading
from dataclasses import dataclass, field

from app.requests import notify

logger = logging.getLogger(__name__)

# Enough to identify what failed without turning the message into a log dump.
MAX_LISTED_FAILURES = 10
MAX_ERROR_CHARS = 120
MAX_BODY_CHARS = 1800

_BATCH_TITLES = {
    "season": ("Stagione completata", "Stagione completata con errori", "Stagione non scaricata"),
    "series": ("Serie completata", "Serie completata con errori", "Serie non scaricata"),
    "anime_all": ("Anime completato", "Anime completato con errori", "Anime non scaricato"),
}


# ── Batch bookkeeping ──────────────────────────────────────────────────────────

@dataclass
class _Batch:
    kind: str
    label: str
    user_id: int | None
    remaining: int
    done: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)


_batches: dict[str, _Batch] = {}
_lock = threading.Lock()


def register(batch_id: str, *, kind: str, label: str, expected: int, user_id: int | None):
    """Open a batch. Must be called *before* the first job is submitted.

    A job can fail in the instant it is created, so registering afterwards would
    let the first result arrive before the batch it belongs to exists.
    """
    with _lock:
        _batches[batch_id] = _Batch(kind=kind, label=label, user_id=user_id, remaining=expected)


def abandon(batch_id: str, count: int):
    """Write off jobs that were never submitted, so the batch can still close."""
    summary = None
    with _lock:
        batch = _batches.get(batch_id)
        if batch is None:
            return
        batch.remaining -= count
        if batch.remaining <= 0:
            summary = _batches.pop(batch_id)
    if summary is not None:
        _announce_batch(summary)


def pending_batches() -> int:
    """Open batches. Only used by tests and for logging."""
    with _lock:
        return len(_batches)


# ── Labels and text ────────────────────────────────────────────────────────────

def _episode_label(job) -> str:
    """A label that stays unambiguous inside a whole-series batch."""
    if job.season is not None and job.episode_number is not None:
        return f"S{int(job.season):02d}E{str(job.episode_number).zfill(2)}"
    if job.episode_number is not None:
        return f"E{job.episode_number}"
    return job.media_label or job.title


def _clean_error(error: str | None) -> str:
    """Trim an error down to something safe to hand to a third party.

    ``job.error`` is str() of whatever the downloader raised and routinely
    carries the source URL, query string and token included. External channels
    are other people's servers.
    """
    text = (error or "errore sconosciuto").strip()
    text = re.sub(r"(https?://[^\s?]+)\?\S*", r"\1", text)
    text = " ".join(text.split())
    if len(text) > MAX_ERROR_CHARS:
        text = text[:MAX_ERROR_CHARS - 1] + "…"
    return text


def _media_title(job) -> str:
    year = f" ({job.year})" if job.year else ""
    return f"{job.title}{year}"


def _failure_lines(failed: list[tuple[str, str]]) -> list[str]:
    lines = [f"• {label} — `{error}`" for label, error in failed[:MAX_LISTED_FAILURES]]
    hidden = len(failed) - MAX_LISTED_FAILURES
    if hidden > 0:
        lines.append(f"…e altri {hidden}.")
    return lines


def _truncate(body: str) -> str:
    return body if len(body) <= MAX_BODY_CHARS else body[:MAX_BODY_CHARS - 1] + "…"


# ── Single downloads ───────────────────────────────────────────────────────────

def _announce_single(job):
    label = _media_title(job)
    recipients = [job.user_id] if job.user_id else []
    # Nobody to address it to means the panel has no accounts, so the bell shows
    # it as the panel's own rather than dropping it.
    panel = not recipients

    if job.status == "done":
        notify.notify(
            notify.DOWNLOAD_COMPLETED,
            f"«{label}» è pronto in libreria.",
            recipients, panel_wide=panel,
            markdown_message=f"«**{label}**» è pronto in libreria.",
            title="Download completato",
        )
        return

    error = _clean_error(job.error)
    notify.notify(
        notify.DOWNLOAD_FAILED,
        f"Il download di «{label}» è fallito: {error}",
        recipients, panel_wide=panel,
        markdown_message=f"«**{label}**»\n`{error}`",
        title="Download fallito",
    )


# ── Batch summaries ────────────────────────────────────────────────────────────

def _announce_batch(batch: _Batch):
    ok, failed, cancelled = len(batch.done), batch.failed, batch.cancelled
    recipients = [batch.user_id] if batch.user_id else []
    panel = not recipients
    total = ok + len(failed) + len(cancelled)
    ok_title, partial_title, none_title = _BATCH_TITLES.get(batch.kind, _BATCH_TITLES["season"])

    counts = f"✅ {ok} scaricati"
    if failed:
        counts += f" · ❌ {len(failed)} falliti"
    if cancelled:
        counts += f" · ⏹ {len(cancelled)} annullati"

    if not failed:
        plain = f"«{batch.label}» completata: {ok} episodi su {total} scaricati."
        body = f"«**{batch.label}**»\n{counts}"
        notify.notify(notify.DOWNLOAD_BATCH_COMPLETED, plain, recipients, panel_wide=panel,
                      markdown_message=_truncate(body), title=ok_title)
        return

    listed = "\n".join(_failure_lines(failed))
    if ok == 0:
        plain = f"«{batch.label}»: nessun episodio scaricato, {len(failed)} falliti."
        body = f"«**{batch.label}**»\n❌ {len(failed)} episodi su {total} falliti.\n\n**Episodi falliti**\n{listed}"
        notify.notify(notify.DOWNLOAD_BATCH_FAILED, plain, recipients, panel_wide=panel,
                      markdown_message=_truncate(body), title=none_title)
        return

    plain = f"«{batch.label}»: {ok} episodi scaricati, {len(failed)} falliti."
    body = f"«**{batch.label}**»\n{counts}\n\n**Episodi falliti**\n{listed}"
    # Neither a success nor a failure, and the colour should say so.
    notify.notify(notify.DOWNLOAD_BATCH_COMPLETED, plain, recipients, panel_wide=panel,
                  markdown_message=_truncate(body), title=partial_title,
                  notify_type=notify.WARNING)


# ── The listener ───────────────────────────────────────────────────────────────

def on_job_finished(job):
    """Called for every job reaching a terminal state, batched or not."""
    if job.batch_id:
        _record_batch_result(job)
        return

    # Cancelling is a decision, not news: whoever pressed the button knows.
    if job.status == "cancelled":
        return

    from app.requests import models

    # A job the request system owns has already been reported by service.py.
    if models.get_by_job_id(job.job_id) is not None:
        return

    _announce_single(job)


def _record_batch_result(job):
    summary = None
    with _lock:
        batch = _batches.get(job.batch_id)
        if batch is None:
            # Registration always precedes submission, so this means the batch
            # already closed — a duplicate terminal callback for one job.
            logger.warning("Job %s reported into unknown batch %s", job.job_id, job.batch_id)
            return
        label = _episode_label(job)
        if job.status == "done":
            batch.done.append(label)
        elif job.status == "cancelled":
            batch.cancelled.append(label)
        else:
            batch.failed.append((label, _clean_error(job.error)))
        batch.remaining -= 1
        if batch.remaining <= 0:
            summary = _batches.pop(job.batch_id)

    # Built and sent outside the lock: delivery is network I/O to third parties.
    if summary is not None:
        _announce_batch(summary)


_listener_registered = False


def register_batch_listener():
    """Wire the job manager to direct-download notifications. Called once, from
    the app lifespan."""
    global _listener_registered
    if _listener_registered:
        return
    from app.jobs import job_manager
    job_manager.add_listener(on_job_finished)
    _listener_registered = True
