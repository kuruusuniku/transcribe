from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...config import AppConfig
from ...state import get_all_jobs, get_job_by_id
from ..deps import get_config

router = APIRouter()

ConfigDep = Annotated[AppConfig, Depends(get_config)]


@router.get("/jobs")
async def list_jobs(cfg: ConfigDep):
    rows = get_all_jobs(cfg.state_db)
    return [dict(r) for r in rows]


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, cfg: ConfigDep):
    row = get_job_by_id(cfg.state_db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return dict(row)


@router.get("/jobs/{job_id}/transcript")
async def get_transcript(job_id: int, cfg: ConfigDep):
    row = get_job_by_id(cfg.state_db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not row["output_dir"]:
        raise HTTPException(status_code=404, detail="No output directory")
    path = Path(row["output_dir"]) / "transcript.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="transcript.md not found")
    return {"content": path.read_text(encoding="utf-8")}


@router.get("/jobs/{job_id}/summary")
async def get_summary(job_id: int, cfg: ConfigDep):
    row = get_job_by_id(cfg.state_db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not row["output_dir"]:
        raise HTTPException(status_code=404, detail="No output directory")
    path = Path(row["output_dir"]) / "summary.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="summary.md not found")
    return {"content": path.read_text(encoding="utf-8")}


class TranscriptUpdateBody(BaseModel):
    content: str


@router.put("/jobs/{job_id}/transcript")
async def update_transcript(job_id: int, body: TranscriptUpdateBody, cfg: ConfigDep):
    row = get_job_by_id(cfg.state_db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    if not row["output_dir"]:
        raise HTTPException(status_code=404, detail="No output directory")
    path = Path(row["output_dir"]) / "transcript.md"
    if not path.exists():
        raise HTTPException(status_code=404, detail="transcript.md not found")
    path.write_text(body.content, encoding="utf-8")
    return {"ok": True}
