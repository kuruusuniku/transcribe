from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .routes import commands, files, jobs
from .routes.glossary import router as glossary_router
from .security import AccessControlMiddleware, install_log_redaction
from . import output_router
from .worker import worker
from .ws import log_stream

_WEB_DIR = Path(__file__).parent
STATIC_DIR = _WEB_DIR / "static"



@asynccontextmanager
async def lifespan(_app: FastAPI):
    if os.environ.get("TRANSCRIBE_WEB_SERVER") == "1":
        # `transcribe web` から起動された場合のみ（テストでは適用しない）
        # ワーカースレッドで実行するコマンドの出力をタスクログに流す
        output_router.install()
        install_log_redaction()
        _app.state.auth_token = os.environ.get("TRANSCRIBE_WEB_TOKEN") or None
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

class _NoCacheStaticFiles(StaticFiles):
    """静的ファイルを毎回検証させる（更新したのに古い画面が出るのを防ぐ）。"""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", _NoCacheStaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})
