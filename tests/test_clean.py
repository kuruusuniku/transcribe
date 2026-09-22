"""clean コマンドのテスト。"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from transcribe.cli import app
from transcribe.pipeline import pipeline_lock
from transcribe.state import init_db, update_status, upsert_job

runner = CliRunner()


@pytest.fixture
def env(tmp_path):
    db = tmp_path / "data" / "state.db"
    init_db(db)
    work = tmp_path / "data" / "work"
    work.mkdir()
    uploads = tmp_path / "data" / "uploads"
    uploads.mkdir()
    cfg = MagicMock()
    cfg.state_db = db
    cfg.work_dir = work
    return cfg, db, work, uploads


def _run(cfg):
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, MagicMock())):
        return runner.invoke(app, ["clean"])


def test_clean_removes_work_files_and_finished_uploads(env):
    cfg, db, work, uploads = env
    (work / "abc.mp3").write_bytes(b"x")

    done_dir = uploads / "done1"
    done_dir.mkdir()
    (done_dir / "講義.mp3").write_bytes(b"x")
    done_id = upsert_job(db, str(done_dir / "講義.mp3"), source_type="local")
    update_status(db, done_id, "done")

    failed_dir = uploads / "failed1"
    failed_dir.mkdir()
    (failed_dir / "講義2.mp3").write_bytes(b"x")
    failed_id = upsert_job(db, str(failed_dir / "講義2.mp3"), source_type="local")
    update_status(db, failed_id, "failed")

    result = _run(cfg)

    assert result.exit_code == 0, result.output
    assert not (work / "abc.mp3").exists()
    assert not done_dir.exists()
    # 未完了（再開に使う）ジョブのアップロードは残す
    assert (failed_dir / "講義2.mp3").exists()


def test_clean_refuses_while_pipeline_running(env):
    cfg, db, work, _ = env
    (work / "abc.mp3").write_bytes(b"x")

    lock = pipeline_lock(db)
    lock.acquire()
    try:
        result = _run(cfg)
    finally:
        lock.release()

    assert result.exit_code == 1
    assert (work / "abc.mp3").exists()
