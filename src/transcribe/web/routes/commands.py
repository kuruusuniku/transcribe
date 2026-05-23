from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, field_validator

from ...config import AppConfig
from ..deps import get_config
from ..runner import start_task, transcribe_cmd

router = APIRouter()

ConfigDep = Annotated[AppConfig, Depends(get_config)]


class RunBody(BaseModel):
    urls: list[str]

    @field_validator("urls")
    @classmethod
    def urls_not_empty(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("urls must not be empty")
        return v


class SyncBody(BaseModel):
    all: bool = False


class SummarizeBody(BaseModel):
    all: bool = False
    job_id: int | None = None


class RerunBody(BaseModel):
    url_or_id: str


@router.post("/run")
async def run_urls(body: RunBody, cfg: ConfigDep):
    from uuid import uuid4

    tmp_id = uuid4().hex
    tmp_file = cfg.work_dir / f"urls_{tmp_id}.txt"
    tmp_file.write_text("\n".join(body.urls), encoding="utf-8")

    cmd = transcribe_cmd("run", "--urls", str(tmp_file))
    task_id = start_task(cmd, cleanup=tmp_file)
    return {"task_id": task_id}


@router.post("/sync")
async def sync_jobs(body: SyncBody, cfg: ConfigDep):
    cmd = transcribe_cmd("sync") + (["--all"] if body.all else [])
    task_id = start_task(cmd)
    return {"task_id": task_id}


@router.post("/summarize")
async def summarize_jobs(body: SummarizeBody, cfg: ConfigDep):
    cmd = transcribe_cmd("summarize")
    if body.all:
        cmd.append("--all")
    if body.job_id is not None:
        cmd += ["--id", str(body.job_id)]
    task_id = start_task(cmd)
    return {"task_id": task_id}


@router.post("/rerun")
async def rerun_job(body: RerunBody, cfg: ConfigDep):
    cmd = transcribe_cmd("rerun", body.url_or_id, "--yes")
    task_id = start_task(cmd)
    return {"task_id": task_id}


class RetryBody(BaseModel):
    job_id: int


class DeleteBody(BaseModel):
    job_id: int
    files: bool = False


class SyncNotionBody(BaseModel):
    all: bool = False
    job_id: int | None = None


@router.post("/retry")
async def retry_job(body: RetryBody):
    cmd = transcribe_cmd("retry", str(body.job_id))
    task_id = start_task(cmd)
    return {"task_id": task_id}


@router.post("/delete")
async def delete_job(body: DeleteBody):
    cmd = transcribe_cmd("delete", str(body.job_id))
    if body.files:
        cmd.append("--files")
    task_id = start_task(cmd)
    return {"task_id": task_id}


@router.post("/sync-notion")
async def sync_notion_jobs(body: SyncNotionBody):
    cmd = transcribe_cmd("sync-notion")
    if body.all:
        cmd.append("--all")
    if body.job_id is not None:
        cmd += ["--id", str(body.job_id)]
    task_id = start_task(cmd)
    return {"task_id": task_id}
