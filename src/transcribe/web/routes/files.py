from __future__ import annotations

import asyncio
import subprocess
import zipfile
from pathlib import Path
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
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


def _unlink_many(*paths: Path) -> None:
    for p in paths:
        p.unlink(missing_ok=True)


@router.post("/convert")
async def convert_file(files: list[UploadFile] = File(...), cfg: AppConfig = Depends(get_config)):
    for upload in files:
        ext = Path(upload.filename or "").suffix.lower()
        if ext != ".m4a":
            raise HTTPException(
                status_code=400,
                detail=f"Only .m4a files are supported for conversion (got: '{ext or 'unknown'}')",
            )

    mp3_results: list[tuple[Path, str]] = []
    m4a_temps: list[Path] = []

    try:
        for upload in files:
            stem = Path(upload.filename or "output").stem
            tmp_m4a = cfg.work_dir / f"{uuid4().hex}.m4a"
            tmp_mp3 = tmp_m4a.with_suffix(".mp3")
            m4a_temps.append(tmp_m4a)

            content = await upload.read()
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
                raise HTTPException(
                    status_code=500,
                    detail=f"ffmpeg conversion failed: {stderr.decode('utf-8', errors='replace')}",
                )

            mp3_results.append((tmp_mp3, stem))

        if len(mp3_results) == 1:
            tmp_mp3, stem = mp3_results[0]
            mp3_bytes = tmp_mp3.read_bytes()
            tmp_mp3.unlink(missing_ok=True)
            return StreamingResponse(
                iter([mp3_bytes]),
                media_type="audio/mpeg",
                headers={"Content-Disposition": "attachment"},
            )

        tmp_zip = cfg.work_dir / f"{uuid4().hex}.zip"
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_STORED) as zf:
            for tmp_mp3, stem in mp3_results:
                zf.write(tmp_mp3, f"{stem}.mp3")

        mp3_paths = [p for p, _ in mp3_results]
        return FileResponse(
            str(tmp_zip),
            filename="converted.zip",
            media_type="application/zip",
            background=BackgroundTask(_unlink_many, tmp_zip, *mp3_paths),
        )

    except HTTPException:
        for p in m4a_temps:
            p.unlink(missing_ok=True)
        for tmp_mp3, _ in mp3_results:
            tmp_mp3.unlink(missing_ok=True)
        raise
