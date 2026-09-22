from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from transcribe.config import (
    AppConfig,
    AudioSeparationConfig,
    GlossaryConfig,
    GoogleDocsConfig,
    LoggingConfig,
    OutputConfig,
    PathsConfig,
    RetryConfig,
    SummarizeConfig,
    TranscriptionConfig,
    VadParameters,
    YoutubeConfig,
)
from transcribe.pipeline import run_pipeline
from transcribe.stages.transcriber import Segment
from transcribe.state import (
    get_job_by_url_or_id,
    get_job_stages,
    get_post_stage_errors,
    init_db,
    reset_for_rerun,
    run_stage,
    upsert_job,
)


@pytest.fixture
def cfg(tmp_path):
    return AppConfig(
        paths=PathsConfig(
            work_dir=tmp_path / "work",
            output_dir=tmp_path / "output",
            state_db=tmp_path / "state.db",
            log_dir=tmp_path / "logs",
        ),
        youtube=YoutubeConfig(),
        audio_separation=AudioSeparationConfig(),
        transcription=TranscriptionConfig(vad_parameters=VadParameters()),
        output=OutputConfig(),
        retry=RetryConfig(max_attempts=2, backoff_seconds=[0]),
        logging=LoggingConfig(),
        google_docs=GoogleDocsConfig(),
        summarize=SummarizeConfig(),
    )


@pytest.fixture
def db(cfg):
    init_db(cfg.state_db)
    return cfg.state_db


def _stages(db, job_id):
    return {r["stage"]: dict(r) for r in get_job_stages(db, job_id)}


# ─── run_stage ────────────────────────────────────────────────────────────


def test_run_stage_records_done_skipped_failed(db):
    job_id = upsert_job(db, "https://example.com/a")

    assert run_stage(db, job_id, "summarize", lambda: "ok") == "ok"
    run_stage(db, job_id, "docs_sync", lambda: None)
    with pytest.raises(RuntimeError):
        run_stage(db, job_id, "notion_sync", lambda: (_ for _ in ()).throw(RuntimeError("401")))

    st = _stages(db, job_id)
    assert st["summarize"]["status"] == "done"
    assert st["docs_sync"]["status"] == "skipped"
    assert st["notion_sync"]["status"] == "failed"
    assert "401" in st["notion_sync"]["error"]
    assert get_post_stage_errors(db, job_id) == "Notion 同期: RuntimeError: 401"


def test_run_stage_counts_attempts_and_clears_error(db):
    job_id = upsert_job(db, "https://example.com/a")
    with pytest.raises(ValueError):
        run_stage(db, job_id, "summarize", lambda: (_ for _ in ()).throw(ValueError("x")))
    run_stage(db, job_id, "summarize", lambda: True)

    st = _stages(db, job_id)["summarize"]
    assert st["attempts"] == 2
    assert st["status"] == "done"
    assert st["error"] is None
    assert get_post_stage_errors(db, job_id) is None


def test_reset_for_rerun_clears_stages(db):
    job_id = upsert_job(db, "https://example.com/a")
    run_stage(db, job_id, "summarize", lambda: True)
    reset_for_rerun(db, job_id)
    assert get_job_stages(db, job_id) == []


# ─── パイプライン: ステージ記録と文字起こしキャッシュ ───────────────────────


def _dl_result(tmp_path):
    audio = tmp_path / "work" / "VIDxxxxxxxx.mp3"
    audio.parent.mkdir(parents=True, exist_ok=True)
    audio.write_bytes(b"x")
    r = MagicMock()
    r.video_id = "VIDxxxxxxxx"
    r.title = "t"
    r.audio_path = audio
    r.upload_date = "20260101"
    return r


def test_pipeline_records_core_stages(cfg, db, tmp_path):
    url = "https://www.youtube.com/watch?v=VIDxxxxxxxx"
    with (
        patch("transcribe.pipeline.download_audio", return_value=_dl_result(tmp_path)),
        patch("transcribe.pipeline.separate_audio", side_effect=lambda p, *a: p),
        patch("transcribe.pipeline.transcribe", return_value=iter([Segment(0, 1, "a", -0.1, 0.0)])),
        patch("transcribe.pipeline.format_outputs", return_value=1),
    ):
        run_pipeline([url], cfg, GlossaryConfig())

    job = get_job_by_url_or_id(db, url)
    st = _stages(db, job["id"])
    assert [st[s]["status"] for s in ("download", "separate", "transcribe", "format")] == ["done"] * 4
    # 完了後はキャッシュを削除
    assert not (cfg.work_dir / "VIDxxxxxxxx.whisper.json").exists()


def test_pipeline_retry_reuses_transcription_cache(cfg, db, tmp_path):
    """出力で失敗した場合、リトライでは Whisper を再実行しない。"""
    url = "https://www.youtube.com/watch?v=VIDxxxxxxxx"
    mock_transcribe = MagicMock(return_value=iter([Segment(0, 1, "a", -0.1, 0.0)]))
    with (
        patch("transcribe.pipeline.download_audio", return_value=_dl_result(tmp_path)),
        patch("transcribe.pipeline.separate_audio", side_effect=lambda p, *a: p),
        patch("transcribe.pipeline.transcribe", mock_transcribe),
        patch("transcribe.pipeline.format_outputs", side_effect=[OSError("disk full"), 1]) as mock_fmt,
        patch("transcribe.pipeline.time.sleep"),
    ):
        run_pipeline([url], cfg, GlossaryConfig())

    assert mock_transcribe.call_count == 1
    assert mock_fmt.call_count == 2
    segments = list(mock_fmt.call_args.kwargs["segments"])
    assert [s.text for s in segments] == ["a"]

    job = get_job_by_url_or_id(db, url)
    st = _stages(db, job["id"])
    assert st["transcribe"]["status"] == "skipped"
    assert st["format"]["status"] == "done"
    assert st["format"]["attempts"] == 2
