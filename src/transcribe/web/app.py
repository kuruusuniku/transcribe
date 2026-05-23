from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routes import commands, files, jobs
from .ws import log_stream

_WEB_DIR = Path(__file__).parent
STATIC_DIR = _WEB_DIR / "static"

app = FastAPI(title="transcribe Web UI", docs_url="/api/docs")

app.include_router(jobs.router, prefix="/api")
app.include_router(commands.router, prefix="/api")
app.include_router(files.router, prefix="/api")
app.include_router(log_stream.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
