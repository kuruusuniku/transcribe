"""rerun コマンドと関連 state 関数のユニットテスト。

実 state.db / data/output/ には一切触れない。
- DB: tmp_path 上の一時 SQLite
- ファイルシステム: tmp_path 上の一時ディレクトリ
- run_pipeline / _load_cfg_and_glossary: unittest.mock でモック
"""
from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from transcribe.cli import app
from transcribe.state import (
    get_job_by_id,
    get_job_by_url_or_id,
    init_db,
    record_error,
    reset_for_rerun,
    update_status,
    upsert_job,
)

runner = CliRunner()

# ─── fixtures ──────────────────────────────────────────────────────────────


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
    """run_pipeline と _load_cfg_and_glossary を同時にモックするコンテキスト。"""
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(fake_cfg, MagicMock())) as ml, \
         patch("transcribe.cli.run_pipeline") as mp:
        yield ml, mp


# ─── state 関数のテスト ────────────────────────────────────────────────────


class TestGetJobByUrlOrId:
    def test_find_by_exact_url(self, db):
        upsert_job(db, "https://example.com/watch?v=A1")
        job = get_job_by_url_or_id(db, "https://example.com/watch?v=A1")
        assert job is not None
        assert job["url"] == "https://example.com/watch?v=A1"

    def test_find_by_video_id(self, db):
        upsert_job(db, "https://example.com/watch?v=B1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=B1")
        update_status(db, j["id"], "done", video_id="B1")

        found = get_job_by_url_or_id(db, "B1")
        assert found is not None
        assert found["video_id"] == "B1"

    def test_url_takes_priority_over_video_id(self, db):
        """URL が完全一致する場合は video_id 検索より優先される"""
        upsert_job(db, "https://example.com/watch?v=PRIO")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=PRIO")
        update_status(db, j["id"], "done", video_id="PRIO")

        found = get_job_by_url_or_id(db, "https://example.com/watch?v=PRIO")
        assert found["url"] == "https://example.com/watch?v=PRIO"

    def test_not_found_returns_none(self, db):
        assert get_job_by_url_or_id(db, "https://notexist.example.com") is None
        assert get_job_by_url_or_id(db, "NOTEXIST") is None


class TestResetForRerun:
    def test_resets_all_fields(self, db):
        upsert_job(db, "https://example.com/watch?v=R1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=R1")
        update_status(db, j["id"], "done", output_dir="/some/output/path")

        reset_for_rerun(db, j["id"])

        updated = get_job_by_id(db, j["id"])
        assert updated["status"] == "queued"
        assert updated["retry_count"] == 0
        assert updated["error_message"] is None
        assert updated["output_dir"] is None

    def test_resets_retry_count_from_failed(self, db):
        upsert_job(db, "https://example.com/watch?v=R2")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=R2")
        record_error(db, j["id"], "error1")
        record_error(db, j["id"], "error2")

        reset_for_rerun(db, j["id"])

        updated = get_job_by_id(db, j["id"])
        assert updated["retry_count"] == 0
        assert updated["status"] == "queued"
        assert updated["error_message"] is None


# ─── rerun コマンドのテスト ────────────────────────────────────────────────


class TestRerunCommand:
    def test_done_job_creates_backup(self, db, fake_cfg, tmp_path):
        """done ジョブ: --yes でバックアップが作成され DB がリセットされる"""
        upsert_job(db, "https://example.com/watch?v=BK1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=BK1")
        output_dir = tmp_path / "2025-01-01_BK1"
        output_dir.mkdir()
        (output_dir / "transcript.md").write_text("test")
        update_status(db, j["id"], "done", video_id="BK1", output_dir=str(output_dir))

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["rerun", "https://example.com/watch?v=BK1", "--yes"])

        assert result.exit_code == 0, result.output
        # バックアップが作成されている
        backups = list(tmp_path.glob("2025-01-01_BK1_backup_*"))
        assert len(backups) == 1
        assert (backups[0] / "transcript.md").exists()
        assert not output_dir.exists()
        # DB がリセットされている
        updated = get_job_by_id(db, j["id"])
        assert updated["status"] == "queued"
        assert updated["retry_count"] == 0
        assert updated["output_dir"] is None
        # パイプラインが呼ばれている
        mock_pipeline.assert_called_once()

    def test_no_backup_deletes_dir(self, db, fake_cfg, tmp_path):
        """--no-backup フラグで既存ディレクトリが削除される"""
        upsert_job(db, "https://example.com/watch?v=NB1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=NB1")
        output_dir = tmp_path / "2025-01-01_NB1"
        output_dir.mkdir()
        update_status(db, j["id"], "done", video_id="NB1", output_dir=str(output_dir))

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(
                app, ["rerun", "https://example.com/watch?v=NB1", "--yes", "--no-backup"]
            )

        assert result.exit_code == 0, result.output
        assert not output_dir.exists()
        assert len(list(tmp_path.glob("2025-01-01_NB1_backup_*"))) == 0
        mock_pipeline.assert_called_once()

    def test_yes_flag_skips_prompt(self, db, fake_cfg, tmp_path):
        """--yes フラグでは stdin を与えなくても確認プロンプトを通過する"""
        upsert_job(db, "https://example.com/watch?v=YF1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=YF1")
        output_dir = tmp_path / "2025-01-01_YF1"
        output_dir.mkdir()
        update_status(db, j["id"], "done", video_id="YF1", output_dir=str(output_dir))

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            # input を与えない — プロンプトが出ると EOF でエラーになるはず
            result = runner.invoke(app, ["rerun", "https://example.com/watch?v=YF1", "--yes"])

        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()

    def test_prompt_abort_on_no(self, db, fake_cfg, tmp_path):
        """確認プロンプトで n を入力したらキャンセルされ、出力は残り DB も変わらない"""
        upsert_job(db, "https://example.com/watch?v=NO1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=NO1")
        output_dir = tmp_path / "2025-01-01_NO1"
        output_dir.mkdir()
        update_status(db, j["id"], "done", video_id="NO1", output_dir=str(output_dir))

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(
                app, ["rerun", "https://example.com/watch?v=NO1"], input="n\n"
            )

        mock_pipeline.assert_not_called()
        assert output_dir.exists()
        updated = get_job_by_id(db, j["id"])
        assert updated["status"] == "done"

    def test_output_dir_missing_still_runs(self, db, fake_cfg, tmp_path):
        """output_dir が DB にあるが実在しない場合も正常に動く"""
        upsert_job(db, "https://example.com/watch?v=ND1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=ND1")
        update_status(
            db, j["id"], "done", video_id="ND1",
            output_dir=str(tmp_path / "nonexistent_dir")
        )

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["rerun", "https://example.com/watch?v=ND1", "--yes"])

        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()
        updated = get_job_by_id(db, j["id"])
        assert updated["status"] == "queued"

    def test_url_not_found_registers_new_job(self, db, fake_cfg):
        """DB に存在しない URL は新規ジョブとして run_pipeline に渡される"""
        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(
                app, ["rerun", "https://example.com/watch?v=NEW1", "--yes"]
            )

        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()
        call_args = mock_pipeline.call_args
        assert call_args[0][0] == ["https://example.com/watch?v=NEW1"]

    def test_find_by_video_id_in_rerun(self, db, fake_cfg, tmp_path):
        """video_id でジョブを指定できる"""
        upsert_job(db, "https://example.com/watch?v=VID1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=VID1")
        update_status(db, j["id"], "done", video_id="VID1")

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["rerun", "VID1", "--yes"])

        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()

    def test_numeric_id_finds_job(self, db, fake_cfg, tmp_path):
        """数字のみを渡すとジョブIDで検索される（URLとして扱われない）"""
        upsert_job(db, "https://example.com/watch?v=NUM1")
        j = get_job_by_url_or_id(db, "https://example.com/watch?v=NUM1")
        update_status(db, j["id"], "done", video_id="NUM1")

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["rerun", str(j["id"]), "--yes"])

        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()
        updated = get_job_by_id(db, j["id"])
        assert updated["status"] == "queued"

    def test_numeric_id_not_found_shows_error(self, db, fake_cfg):
        """存在しない数字IDを渡すとエラーメッセージが出て終了する（新規登録されない）"""
        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["rerun", "9999", "--yes"])

        assert result.exit_code == 1
        assert "9999" in result.output
        mock_pipeline.assert_not_called()
