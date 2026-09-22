from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ...config import AppConfig
from ...pipeline import enabled_post_stages
from ...state import POST_STAGES, get_all_jobs, get_all_stage_summaries, get_job_by_id, get_job_stages
from ..deps import get_config

router = APIRouter()

ConfigDep = Annotated[AppConfig, Depends(get_config)]


_IN_PROGRESS = ("downloading", "separating", "transcribing", "formatting")


def _read_meta(output_dir: str | None) -> dict:
    if not output_dir:
        return {}
    path = Path(output_dir) / "meta.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _attention(job: dict, stages: dict, enabled_post: list[str]) -> list[str]:
    """ユーザーの対応が必要な理由（一覧の「要対応」判定に使う）。"""
    reasons: list[str] = []
    if job["status"] == "failed":
        reasons.append("処理に失敗しました")
        return reasons
    if job["status"] != "done":
        return reasons
    for stage in POST_STAGES:
        if stages.get(stage, {}).get("status") == "failed":
            reasons.append(f"{STAGE_NAMES[stage]}に失敗しました")
    if job.get("summary_truncated"):
        reasons.append("まとめが途中で切れています")
    elif "summarize" in enabled_post and not job.get("summarized_at") and "summarize" not in stages:
        reasons.append("まとめがありません")
    # 低信頼セグメント（要確認箇所）は必須の対応ではないため attention には含めず、一覧で件数だけ表示する
    return reasons


STAGE_NAMES = {"summarize": "まとめ生成", "docs_sync": "Google Docs 同期", "notion_sync": "Notion 同期"}


@router.get("/jobs")
async def list_jobs(cfg: ConfigDep):
    rows = get_all_jobs(cfg.state_db)
    summaries = get_all_stage_summaries(cfg.state_db)
    enabled_post = enabled_post_stages(cfg)
    result = []
    for r in rows:
        job = dict(r)
        meta = _read_meta(job["output_dir"])
        stages = summaries.get(job["id"], {})
        job["stages"] = stages
        job["recording_date"] = meta.get("recording_date")
        job["low_confidence_count"] = meta.get("low_confidence_count", 0)
        job["summary_truncated"] = bool(
            job["output_dir"] and (Path(job["output_dir"]) / "summary.truncated.md").exists()
        )
        job["in_progress"] = job["status"] in _IN_PROGRESS or job["status"] == "queued"
        job["progress"] = stages.get("transcribe", {}).get("progress") if job["status"] == "transcribing" else None
        job["attention"] = _attention(job, stages, enabled_post)
        result.append(job)
    return result


@router.get("/jobs/{job_id}")
async def get_job(job_id: int, cfg: ConfigDep):
    row = get_job_by_id(cfg.state_db, job_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job = dict(row)
    out = Path(job["output_dir"]) if job["output_dir"] else None
    job["has_transcript"] = bool(out and (out / "transcript.md").exists())
    job["has_summary"] = bool(out and (out / "summary.md").exists())
    job["summary_truncated"] = bool(out and (out / "summary.truncated.md").exists())
    job["notion_url"] = None
    if out and (out / "notion.json").exists():
        try:
            page_id = json.loads((out / "notion.json").read_text(encoding="utf-8")).get("page_id", "")
            job["notion_url"] = f"https://www.notion.so/{page_id.replace('-', '')}" if page_id else None
        except Exception:
            pass
    return job


@router.get("/jobs/{job_id}/segments")
async def get_segments(job_id: int, cfg: ConfigDep):
    row = get_job_by_id(cfg.state_db, job_id)
    if row is None or not row["output_dir"]:
        raise HTTPException(status_code=404, detail="Job not found")
    path = Path(row["output_dir"]) / "segments.json"
    if not path.exists():
        raise HTTPException(status_code=404, detail="segments.json not found")
    return json.loads(path.read_text(encoding="utf-8"))


@router.get("/jobs/{job_id}/stages")
async def get_stages(job_id: int, cfg: ConfigDep):
    if get_job_by_id(cfg.state_db, job_id) is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return [dict(r) for r in get_job_stages(cfg.state_db, job_id)]


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
