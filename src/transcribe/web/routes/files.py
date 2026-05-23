from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from ...config import AppConfig
from ..deps import get_config
from ..runner import start_task, transcribe_cmd

router = APIRouter()

ConfigDep = Annotated[AppConfig, Depends(get_config)]

_SUPPORTED_AUDIO = frozenset({".mp3", ".m4a"})


@router.post("/run/file")
async def run_file(audio: UploadFile = File(...), cfg: AppConfig = Depends(get_config)):
    ext = Path(audio.filename or "").suffix.lower()
    if ext not in _SUPPORTED_AUDIO:
        allowed = ", ".join(sorted(_SUPPORTED_AUDIO))
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file type. Allowed: {allowed}",
        )

    tmp_path = cfg.work_dir / f"{uuid4().hex}{ext}"
    content = await audio.read()
    tmp_path.write_bytes(content)

    cmd = transcribe_cmd("file", str(tmp_path))
    task_id = start_task(cmd, cleanup=tmp_path)
    return {"task_id": task_id}


@router.post("/convert")
async def convert_file(audio: UploadFile = File(...), cfg: AppConfig = Depends(get_config)):
    ext = Path(audio.filename or "").suffix.lower()
    if ext != ".m4a":
        raise HTTPException(status_code=400, detail="Only .m4a files are supported for conversion")

    stem = Path(audio.filename or "output").stem
    tmp_m4a = cfg.work_dir / f"{uuid4().hex}.m4a"
    tmp_mp3 = tmp_m4a.with_suffix(".mp3")

    content = await audio.read()
    tmp_m4a.write_bytes(content)

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(tmp_m4a),
        "-vn", "-c:a", "libmp3lame", "-q:a", "2", str(tmp_mp3),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    tmp_m4a.unlink(missing_ok=True)

    if proc.returncode != 0:
        tmp_mp3.unlink(missing_ok=True)
        raise HTTPException(
            status_code=500,
            detail=f"ffmpeg conversion failed: {stderr.decode('utf-8', errors='replace')}",
        )

    return FileResponse(
        str(tmp_mp3),
        filename=f"{stem}.mp3",
        media_type="audio/mpeg",
        background=BackgroundTask(tmp_mp3.unlink, missing_ok=True),
    )
