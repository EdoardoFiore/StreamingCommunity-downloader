import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import db
from app.auth import models as auth_models
from app.auth import router as auth_router
from app.auth import session as auth_session
from app.auth import users_router
from app.auth.deps import AuthMiddleware
from app.jobs import job_manager
from app.requests import router as requests_router, service as requests_service
from app.schedule import ScheduleStore
from app.config import AUTH_ENABLED, SCHEDULE_FILE
from app.routers import domain, search, tv, downloads, progress, files, images, anime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

BASE_DIR = Path(__file__).parent
DOCS_DIR = Path(__file__).parent.parent / "docs"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.run_migrations()
    auth_session.purge_expired()
    requests_service.register_job_listener()
    # Before anything can approve or complete a request: any row still
    # "approved" or "downloading" from a previous run has no in-memory worker
    # left, and never will — it needs recovering before the app is reachable.
    requests_service.reconcile_orphaned_requests()
    store = ScheduleStore(SCHEDULE_FILE)
    job_manager.set_schedule_store(store)
    job_manager.load_scheduled_from_store()
    job_manager.set_loop(asyncio.get_event_loop())
    yield


app = FastAPI(title="StreamingCommunity Web Panel", lifespan=lifespan)

# Authentication is enforced here, once, for every request. Endpoints are closed
# by default; app/auth/deps.py holds the explicit public allowlist.
app.add_middleware(AuthMiddleware)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/docs", StaticFiles(directory=str(DOCS_DIR)), name="docs")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app.include_router(auth_router.router)
app.include_router(users_router.router)
app.include_router(requests_router.router)
app.include_router(requests_router.notifications_router)
app.include_router(domain.router)
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
