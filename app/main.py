import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse

from app.jobs import job_manager
from app.schedule import ScheduleStore
from app.config import SCHEDULE_FILE
from app.routers import domain, search, tv, downloads, progress, files, images, anime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

BASE_DIR = Path(__file__).parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = ScheduleStore(SCHEDULE_FILE)
    job_manager.set_schedule_store(store)
    job_manager.load_scheduled_from_store()
    job_manager.set_loop(asyncio.get_event_loop())
    yield


app = FastAPI(title="StreamingCommunity Web Panel", lifespan=lifespan)

app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

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
