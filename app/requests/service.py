"""Request lifecycle: creation, approval, execution, completion.

Approval returns to the caller immediately and does its work on the download
thread pool. Re-resolving a source is several HTTP round-trips and the download
that follows can run for an hour — neither belongs on an HTTP request.
"""

import json
import logging
import threading

from app.requests import models, notify, resolver

logger = logging.getLogger(__name__)

# Resolution runs on its own small pool rather than the download executor, so a
# saturated download queue cannot stop approvals from being processed.
_resolution_pool = None
_pool_lock = threading.Lock()


def _pool():
    global _resolution_pool
    with _pool_lock:
        if _resolution_pool is None:
            from concurrent.futures import ThreadPoolExecutor
            _resolution_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="approval")
    return _resolution_pool


def _label(request: models.Request) -> str:
    if request.media_type == resolver.EPISODE:
        return f"{request.title} S{request.season:02d}E{request.episode_number}"
    if request.media_type == resolver.ANIME:
        return f"{request.title} E{request.episode_number}"
    return request.title


def _tracks_label(request: models.Request) -> str:
    audio = ", ".join(request.audio_languages) or "originale"
    return f"audio {audio}"


# ── Creation ───────────────────────────────────────────────────────────────────

def create_request(*, requested_by: int, **fields) -> tuple[models.Request, bool]:
    """Create a request, joining an identical open one if there is one.

    ``requested_by`` comes from the session, never from the request body.
    """
    request, created = models.create(requested_by=requested_by, **fields)

    if not created:
        # Someone already asked for exactly this, tracks included. One download,
        # both users told.
        notify.notify(
            notify.REQUEST_JOINED,
            f"«{_label(request)}» era già stato richiesto: sarai avvisato insieme agli altri.",
            [requested_by],
            request.id,
        )
        others = [uid for uid in models.subscribers(request.id) if uid != requested_by]
        notify.notify(
            notify.REQUEST_JOINED,
            f"Un altro utente ha richiesto «{_label(request)}»: resta un solo download.",
            others,
            request.id,
        )
        return request, False

    # Already downloaded? Then there is nothing to queue.
    try:
        already_there = resolver.is_in_library(request)
    except Exception:
        logger.exception("Library check failed for request %s", request.id)
        already_there = False

    if already_there:
        request = models.transition(
            request.id, models.AVAILABLE, output_path=resolver.destination_path(request)
        ) or request
        notify.notify_subscribers(
            notify.REQUEST_AVAILABLE,
            f"«{_label(request)}» è già presente in libreria.",
            request,
        )
        return request, True

    notify.notify_approvers(
        notify.REQUEST_CREATED,
        f"{models.username(requested_by)} ha richiesto «{_label(request)}» ({_tracks_label(request)}).",
        request,
    )
    return request, True


# ── Approval ───────────────────────────────────────────────────────────────────

def approve(request_id: int, decided_by: int) -> models.Request:
    """Claim the request and hand the real work to a worker thread."""
    request = models.transition(
        request_id, models.APPROVED,
        decided_by=decided_by, decided_at=models.now_iso(), problem=None, denial_reason=None,
    )
    if request is None:
        current = models.get(request_id)
        if current is None:
            raise LookupError("Richiesta non trovata")
        raise models.InvalidTransition(current.status, models.APPROVED)

    notify.notify_subscribers(
        notify.REQUEST_APPROVED,
        f"«{_label(request)}» è stata approvata, il download sta per partire.",
        request,
    )
    _pool().submit(_execute, request.id)
    return request


def _execute(request_id: int):
    """Re-resolve, re-check the library, then start the download.

    Runs on a worker thread whose Future nobody reads, so *nothing* here may
    raise out: an escaping exception would vanish without a log and leave the
    request sitting in `approved` forever, looking approved but never starting.
    """
    try:
        _execute_inner(request_id)
    except Exception as exc:
        logger.exception("Unhandled failure executing request %s", request_id)
        request = models.get(request_id)
        if request is not None and request.status == models.APPROVED:
            _park(request, f"error: {exc}", exc)


def _execute_inner(request_id: int):
    request = models.get(request_id)
    if request is None or request.status != models.APPROVED:
        return

    try:
        resolution = resolver.resolve(request)
    except resolver.ResolutionError as exc:
        _park(request, exc.problem, exc)
        return
    except Exception as exc:
        logger.exception("Unexpected failure resolving request %s", request_id)
        _park(request, f"error: {exc}", exc)
        return

    models.update_fields(request_id, available_snapshot=json.dumps(resolution.available))

    if resolver.is_in_library(request):
        finished = models.transition(
            request_id, models.COMPLETED, output_path=resolver.destination_path(request)
        )
        if finished:
            notify.notify_subscribers(
                notify.REQUEST_AVAILABLE,
                f"«{_label(request)}» era già in libreria: nessun download necessario.",
                finished,
            )
        return

    job_id = resolution.submit()

    # Claiming the work and recording the job is one atomic step: a second
    # process that got this far finds the row already in downloading and stops.
    claimed = models.transition(request_id, models.DOWNLOADING, job_id=job_id)
    if claimed is None:
        logger.warning(
            "Request %s was no longer approved when the job started; cancelling job %s",
            request_id, job_id,
        )
        from app.jobs import job_manager
        job_manager.cancel(job_id)
        return

    notify.notify_subscribers(
        notify.REQUEST_DOWNLOADING, f"Download di «{_label(claimed)}» iniziato.", claimed
    )

    # The job was handed to the executor before its id could be written above, so
    # a fast failure could have fired the completion listener while
    # get_by_job_id() still found nothing. Re-check now that the id is stored.
    from app.jobs import job_manager
    job = job_manager.get(job_id)
    if job is not None and job.status in ("done", "error", "cancelled"):
        on_job_finished(job)


def _park(request: models.Request, problem: str, exc: Exception):
    """Send a request back to a human instead of guessing."""
    parked = models.transition(request.id, models.NEEDS_ATTENTION, problem=problem)
    if parked is None:
        logger.warning("Could not park request %s (status changed): %s", request.id, problem)
        return
    logger.warning("Request %s needs attention: %s", request.id, problem)
    notify.notify_approvers(
        notify.REQUEST_NEEDS_ATTENTION,
        f"«{_label(parked)}» richiede attenzione: {problem}",
        parked,
    )
    notify.notify_subscribers(
        notify.REQUEST_NEEDS_ATTENTION,
        f"«{_label(parked)}» è in attesa di una verifica manuale.",
        parked,
    )


# ── Denial and cancellation ────────────────────────────────────────────────────

def deny(request_id: int, decided_by: int, reason: str) -> models.Request:
    request = models.transition(
        request_id, models.DENIED,
        decided_by=decided_by, decided_at=models.now_iso(), denial_reason=reason,
    )
    if request is None:
        current = models.get(request_id)
        if current is None:
            raise LookupError("Richiesta non trovata")
        raise models.InvalidTransition(current.status, models.DENIED)

    notify.notify_subscribers(
        notify.REQUEST_DENIED, f"«{_label(request)}» è stata rifiutata: {reason}", request
    )
    return request


def cancel(request_id: int, cancelled_by: int) -> models.Request:
    request = models.transition(request_id, models.CANCELLED, decided_by=cancelled_by)
    if request is None:
        current = models.get(request_id)
        if current is None:
            raise LookupError("Richiesta non trovata")
        raise models.InvalidTransition(current.status, models.CANCELLED)

    if request.job_id:
        from app.jobs import job_manager
        job_manager.cancel(request.job_id)
    return request


# ── Job completion ─────────────────────────────────────────────────────────────

def on_job_finished(job):
    """Bridge from the job manager's terminal states to request states."""
    request = models.get_by_job_id(job.job_id)
    if request is None or request.status != models.DOWNLOADING:
        return

    if job.status == "done":
        finished = models.transition(
            request.id, models.COMPLETED, output_path=job.output_path
        )
        if finished:
            notify.notify_subscribers(
                notify.REQUEST_COMPLETED,
                f"«{_label(finished)}» è pronto in libreria.",
                finished,
            )
        return

    if job.status == "cancelled":
        models.transition(request.id, models.CANCELLED)
        return

    failed = models.transition(
        request.id, models.FAILED, problem=f"download_failed: {job.error or 'errore sconosciuto'}"
    )
    if failed:
        notify.notify_subscribers(
            notify.REQUEST_FAILED,
            f"Il download di «{_label(failed)}» è fallito: {job.error or 'errore sconosciuto'}",
            failed,
        )
        notify.notify_approvers(
            notify.REQUEST_FAILED,
            f"Download fallito per «{_label(failed)}»: {job.error or 'errore sconosciuto'}",
            failed,
        )


_listener_registered = False


def register_job_listener():
    """Wire the job manager to request state. Called once, from the app lifespan."""
    global _listener_registered
    if _listener_registered:
        return
    from app.jobs import job_manager
    job_manager.add_listener(on_job_finished)
    _listener_registered = True


# ── Startup reconciliation ──────────────────────────────────────────────────────

def reconcile_orphaned_requests() -> int:
    """Recover requests an in-memory worker was still handling when the
    process last stopped — a crash, a deploy, a plain restart.

    Both the resolution pool and the job manager start empty on every launch:
    nothing is or ever will be working on a row still sitting in `approved` or
    `downloading`. Without this, such a row stays open forever — it still
    holds the content key, so the same film or episode can never be requested
    again, and no amount of restarting fixes it on its own, because nothing
    ever looks at these rows to notice they are orphaned.

    `approved` (resolution never finished) goes back to `needs_attention`, so
    a human re-approves or corrects it — the state machine allows that path.
    `downloading` cannot reach `needs_attention` (there is no "resume" state
    for a job that no longer exists in memory), so it goes to `failed`, the
    same outcome a job that errored out reaches via on_job_finished(); the
    partially-downloaded temp files were already cleaned up on the previous
    process's own shutdown path, so there is nothing left to resume anyway.

    Called once from the app lifespan, before the app starts accepting
    requests, so nothing can approve or complete one of these rows out from
    under this pass.
    """
    recovered = 0

    for request in models.list_all((models.APPROVED,)):
        parked = models.transition(
            request.id, models.NEEDS_ATTENTION,
            problem="interrotta: il pannello è stato riavviato durante la risoluzione",
        )
        if parked is None:
            continue
        recovered += 1
        logger.warning(
            "Recovered request %s from an interrupted approval (restart)", request.id
        )
        notify.notify_approvers(
            notify.REQUEST_NEEDS_ATTENTION,
            f"«{_label(parked)}» è da verificare: un riavvio del pannello ha interrotto l'elaborazione.",
            parked,
        )
        notify.notify_subscribers(
            notify.REQUEST_NEEDS_ATTENTION,
            f"«{_label(parked)}» è in attesa di una verifica: un riavvio del pannello ha interrotto l'elaborazione.",
            parked,
        )

    for request in models.list_all((models.DOWNLOADING,)):
        failed = models.transition(
            request.id, models.FAILED,
            problem="interrotta: il pannello è stato riavviato durante il download",
        )
        if failed is None:
            continue
        recovered += 1
        logger.warning(
            "Recovered request %s from an interrupted download (restart)", request.id
        )
        notify.notify_subscribers(
            notify.REQUEST_FAILED,
            f"Il download di «{_label(failed)}» è stato interrotto da un riavvio del pannello.",
            failed,
        )
        notify.notify_approvers(
            notify.REQUEST_FAILED,
            f"Download interrotto per «{_label(failed)}»: il pannello si è riavviato durante il download.",
            failed,
        )

    if recovered:
        logger.warning("Startup reconciliation recovered %d orphaned request(s)", recovered)
    return recovered
