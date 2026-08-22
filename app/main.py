import asyncio
import hashlib
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import __version__, db, downloads_notify
from app.auth import models as auth_models
from app.auth import router as auth_router
from app.auth import session as auth_session
from app.auth import users_router
from app.auth.deps import AuthMiddleware
from app.core import domain_recovery
from app.jobs import job_manager
from app.requests import router as requests_router, service as requests_service
from app.watches import poller as watch_poller, router as watches_router
from app.schedule import ScheduleStore
from app.config import AUTH_ENABLED, SCHEDULE_FILE
from app.routers import (
    domain, search, tv, downloads, progress, files, images, anime, notification_channels,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"


@lru_cache(maxsize=None)
def _asset_version(name: str) -> str:
    """Short content hash of a static file. Empty when the file is missing.

    Computed once per process: the files are baked into the image and cannot
    change while it runs.
    """
    try:
        return hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
    except OSError:
        logger.warning("Static asset not found, serving it unversioned: %s", name)
        return ""


def asset(name: str) -> str:
    """URL for a static file, carrying a hash of its contents.

    Templates must go through this rather than hardcoding ``/static/...``. The
    URL is the only thing an intermediate cache is guaranteed to key on: with a
    fixed path, a reverse proxy is free to keep serving the previous ``app.js``
    against freshly deployed HTML for as long as its own heuristics say, which
    looks exactly like a deploy that did nothing.
    """
    version = _asset_version(name)
    return f"/static/{name}?v={version}" if version else f"/static/{name}"


class VersionedStaticFiles(StaticFiles):
    """Static files with an explicit caching policy.

    Starlette sends only ``ETag``/``Last-Modified`` and no ``Cache-Control``,
    which leaves the decision to each intermediary. Requests carrying a version
    query (everything ``asset()`` emits) may be kept indefinitely, since a
    changed file means a changed URL. Anything else must be revalidated — the
    ETag keeps that cheap — so an unversioned path can never pin a stale file.
    """

    async def get_response(self, path: str, scope):
        response = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned else "no-cache"
        )
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.run_migrations()
    auth_session.purge_expired()
    requests_service.register_job_listener()
    # Second listener, registered after the request one so a request's own row is
    # updated first: this one only speaks up for jobs no request owns.
    downloads_notify.register_batch_listener()
    # Before anything can approve or complete a request: any row still
    # "approved" or "downloading" from a previous run has no in-memory worker
    # left, and never will — it needs recovering before the app is reachable.
    requests_service.reconcile_orphaned_requests()
    store = ScheduleStore(SCHEDULE_FILE)
    job_manager.set_schedule_store(store)
    job_manager.load_scheduled_from_store()
    job_manager.set_loop(asyncio.get_event_loop())
    # Started last: a new episode found by the poller becomes an approval, which
    # submits a job, so the job manager has to be fully wired first.
    poller_task = asyncio.create_task(watch_poller.watch_poller_loop())
    # Independent of the poller: this one watches the source itself rather than
    # anything in it, and both sleep before their first pass so the lifespan
    # never does network I/O.
    domain_task = asyncio.create_task(domain_recovery.domain_watch_loop())
    try:
        yield
    finally:
        poller_task.cancel()
        domain_task.cancel()


app = FastAPI(title="StreamingCommunity Web Panel", version=__version__, lifespan=lifespan)

# Authentication is enforced here, once, for every request. Endpoints are closed
# by default; app/auth/deps.py holds the explicit public allowlist.
app.add_middleware(AuthMiddleware)

app.mount("/static", VersionedStaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
templates.env.globals["asset"] = asset

app.include_router(auth_router.router)
app.include_router(users_router.router)
app.include_router(requests_router.router)
app.include_router(requests_router.notifications_router)
app.include_router(domain.router)
app.include_router(notification_channels.router)
app.include_router(watches_router.router)
app.include_router(search.router)
app.include_router(tv.router)
app.include_router(downloads.router)
app.include_router(progress.router)
app.include_router(files.router)
app.include_router(images.router)
app.include_router(anime.router)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    """Login and first-run setup. Public; redirects away once signed in."""
    if not AUTH_ENABLED or (
        getattr(request.state, "user", None) is not None and auth_models.setup_done()
    ):
        return RedirectResponse("/", status_code=302)
    return templates.TemplateResponse(request=request, name="login.html")
