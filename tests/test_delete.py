"""delete コマンドと delete_job state 関数のユニットテスト。"""
from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from transcribe.cli import app
from transcribe.state import (
    delete_job,
    get_job_by_id,
    get_job_by_url_or_id,
    init_db,
    update_status,
    upsert_job,
)

runner = CliRunner()


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "state.db"
    init_db(db_path)
    return db_path


@pytest.fixture
def fake_cfg(db):
    cfg = MagicMock()
    cfg.state_db = db
    return cfg


@contextmanager
def _mock_env(fake_cfg):
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(fake_cfg, MagicMock())):
        yield


# ─── state 関数のテスト ────────────────────────────────────────────────────


class TestDeleteJob:
    def test_deletes_record(self, db):
        upsert_job(db, "https://example.com/watch?v=D1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=D1")
        job_id = j["id"]

        delete_job(db, job_id)

        assert get_job_by_id(db, job_id) is None

    def test_delete_nonexistent_is_noop(self, db):
        delete_job(db, 9999)


# ─── delete コマンドのテスト ──────────────────────────────────────────────


class TestDeleteCommand:
    def test_deletes_db_record(self, db, fake_cfg):
        upsert_job(db, "https://example.com/watch?v=DC1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=DC1")
        job_id = j["id"]

        with _mock_env(fake_cfg):
            result = runner.invoke(app, ["delete", str(job_id)])

        assert result.exit_code == 0, result.output
        assert get_job_by_id(db, job_id) is None

    def test_files_flag_removes_output_dir(self, db, fake_cfg, tmp_path):
        upsert_job(db, "https://example.com/watch?v=DC2")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=DC2")
        output_dir = tmp_path / "output_DC2"
        output_dir.mkdir()
        (output_dir / "transcript.md").write_text("data")
        update_status(db, j["id"], "done", output_dir=str(output_dir))

        with _mock_env(fake_cfg):
            result = runner.invoke(app, ["delete", str(j["id"]), "--files"], input="y\n")

        assert result.exit_code == 0, result.output
        assert not output_dir.exists()
        assert get_job_by_id(db, j["id"]) is None

    def test_no_files_flag_preserves_output_dir(self, db, fake_cfg, tmp_path):
        upsert_job(db, "https://example.com/watch?v=DC3")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=DC3")
        output_dir = tmp_path / "output_DC3"
        output_dir.mkdir()
        update_status(db, j["id"], "done", output_dir=str(output_dir))

        with _mock_env(fake_cfg):
            result = runner.invoke(app, ["delete", str(j["id"])])

        assert result.exit_code == 0, result.output
        assert output_dir.exists()

    def test_nonexistent_id_shows_error(self, db, fake_cfg):
        with _mock_env(fake_cfg):
            result = runner.invoke(app, ["delete", "9999"])

        assert result.exit_code == 1
        assert "9999" in result.output

    def test_files_flag_with_null_output_dir_does_not_crash(self, db, fake_cfg):
        upsert_job(db, "https://example.com/watch?v=DC4")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=DC4")

        with _mock_env(fake_cfg):
            result = runner.invoke(app, ["delete", str(j["id"]), "--files"])

        assert result.exit_code == 0, result.output
        assert get_job_by_id(db, j["id"]) is None

    def test_files_flag_with_missing_dir_warns_but_succeeds(self, db, fake_cfg, tmp_path):
        upsert_job(db, "https://example.com/watch?v=DC5")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=DC5")
        update_status(db, j["id"], "done", output_dir=str(tmp_path / "nonexistent"))

        with _mock_env(fake_cfg):
            result = runner.invoke(app, ["delete", str(j["id"]), "--files"])

        assert result.exit_code == 0, result.output
        assert get_job_by_id(db, j["id"]) is None
