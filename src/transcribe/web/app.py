from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routes import commands, files, jobs
from .routes.glossary import router as glossary_router
from .security import AccessControlMiddleware
from .worker import worker
from .ws import log_stream

_WEB_DIR = Path(__file__).parent
STATIC_DIR = _WEB_DIR / "static"



@asynccontextmanager
async def lifespan(_app: FastAPI):
    worker.ensure_started()
    yield


app = FastAPI(title="transcribe Web UI", docs_url="/api/docs", lifespan=lifespan)
app.state.auth_token = None  # cli の web コマンドで設定
app.add_middleware(AccessControlMiddleware)

app.include_router(jobs.router, prefix="/api")
app.include_router(commands.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(glossary_router, prefix="/api")
app.include_router(log_stream.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
