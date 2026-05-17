"""ローカル MP3 ファイル文字起こし機能のユニットテスト。

実 state.db / data/output/ には一切触れない。
- DB: tmp_path 上の一時 SQLite
- ファイルシステム: tmp_path 上の一時ディレクトリ
- run_pipeline / _load_cfg_and_glossary: unittest.mock でモック
"""
from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

from transcribe.cli import app, _validate_local_path
from transcribe.config import (
    AppConfig,
    AudioSeparationConfig,
    GlossaryConfig,
    LoggingConfig,
    OutputConfig,
    PathsConfig,
    RetryConfig,
    TranscriptionConfig,
    YoutubeConfig,
)
from transcribe.pipeline import _copy_local_file, is_local_source, run_pipeline
from transcribe.state import (
    get_job_by_id,
    get_job_by_url_or_id,
    init_db,
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


@pytest.fixture
def cfg(tmp_path):
    (tmp_path / "work").mkdir()
    (tmp_path / "output").mkdir()
    return AppConfig(
        paths=PathsConfig(
            work_dir=tmp_path / "work",
            output_dir=tmp_path / "output",
            state_db=tmp_path / "state.db",
            log_dir=tmp_path / "logs",
        ),
        youtube=YoutubeConfig(),
        audio_separation=AudioSeparationConfig(),
        transcription=TranscriptionConfig(),
        output=OutputConfig(),
        retry=RetryConfig(max_attempts=3, backoff_seconds=[0, 0, 0]),
        logging=LoggingConfig(),
    )


@pytest.fixture
def mp3_file(tmp_path):
    f = tmp_path / "keiko_20260510.mp3"
    f.write_bytes(b"\xff\xfb\x90\x00" * 100)
    return f


@contextmanager
def _mock_env(fake_cfg):
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(fake_cfg, MagicMock())) as ml, \
         patch("transcribe.cli.run_pipeline") as mp:
        yield ml, mp


# ─── is_local_source() テスト ─────────────────────────────────────────────


class TestIsLocalSource:
    def test_http_url_is_not_local(self):
        assert is_local_source("https://www.youtube.com/watch?v=abc") is False

    def test_http_url_is_not_local_http(self):
        assert is_local_source("http://example.com") is False

    def test_file_path_is_local(self):
        assert is_local_source(r"C:\Users\sttmn\recordings\keiko.mp3") is True

    def test_relative_path_is_local(self):
        assert is_local_source("recordings/keiko.mp3") is True


# ─── state.py source_type テスト ──────────────────────────────────────────


class TestSourceType:
    def test_upsert_local_job(self, db):
        job_id = upsert_job(db, r"C:\test\keiko.mp3", source_type="local")
        job = get_job_by_id(db, job_id)
        assert job["source_type"] == "local"

    def test_upsert_youtube_job_default(self, db):
        job_id = upsert_job(db, "https://www.youtube.com/watch?v=abc")
        job = get_job_by_id(db, job_id)
        assert job["source_type"] == "youtube"

    def test_migration_existing_jobs_default_to_youtube(self, db):
        """既存ジョブ（source_type 未指定）は youtube がデフォルト。"""
        job_id = upsert_job(db, "https://example.com/watch?v=OLD1")
        job = get_job_by_id(db, job_id)
        assert job["source_type"] == "youtube"


# ─── _validate_local_path() テスト ────────────────────────────────────────


class TestValidateLocalPath:
    def test_valid_mp3(self, mp3_file):
        result = _validate_local_path(str(mp3_file))
        assert result == str(mp3_file.resolve())

    def test_nonexistent_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            _validate_local_path(str(tmp_path / "nonexistent.mp3"))

    def test_wav_file_raises(self, tmp_path):
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"\x00" * 100)
        with pytest.raises(ValueError, match="MP3 のみ"):
            _validate_local_path(str(wav))


# ─── _copy_local_file() テスト ────────────────────────────────────────────


class TestCopyLocalFile:
    def test_copies_file_to_work_dir(self, mp3_file, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        result = _copy_local_file(str(mp3_file), work_dir)
        assert result.exists()
        assert result.parent == work_dir
        assert result.name == mp3_file.name

    def test_nonexistent_source_raises(self, tmp_path):
        work_dir = tmp_path / "work"
        work_dir.mkdir()
        with pytest.raises(FileNotFoundError):
            _copy_local_file(str(tmp_path / "nonexistent.mp3"), work_dir)


# ─── CLI file サブコマンド テスト ──────────────────────────────────────────


class TestFileCommand:
    def test_valid_mp3_calls_pipeline(self, mp3_file, fake_cfg):
        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["file", str(mp3_file)])
        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()
        call_args = mock_pipeline.call_args[0]
        assert len(call_args[0]) == 1
        assert call_args[0][0] == str(mp3_file.resolve())

    def test_nonexistent_file_exits_1(self, fake_cfg, tmp_path):
        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["file", str(tmp_path / "nonexistent.mp3")])
        assert result.exit_code == 1
        mock_pipeline.assert_not_called()

    def test_wav_file_exits_1(self, fake_cfg, tmp_path):
        wav = tmp_path / "test.wav"
        wav.write_bytes(b"\x00" * 100)
        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["file", str(wav)])
        assert result.exit_code == 1
        mock_pipeline.assert_not_called()


# ─── CLI run の自動判別テスト ──────────────────────────────────────────────


class TestRunAutoDetection:
    def test_mixed_urls_and_local_files(self, fake_cfg, tmp_path, mp3_file):
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text(
            f"https://www.youtube.com/watch?v=abc\n{mp3_file}\n# comment\n\n",
            encoding="utf-8",
        )
        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["run", "--urls", str(urls_file)])
        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()
        entries = mock_pipeline.call_args[0][0]
        assert len(entries) == 2
        assert entries[0] == "https://www.youtube.com/watch?v=abc"
        assert entries[1] == str(mp3_file.resolve())

    def test_invalid_local_path_skipped_with_error(self, fake_cfg, tmp_path):
        urls_file = tmp_path / "urls.txt"
        urls_file.write_text(
            f"{tmp_path / 'nonexistent.mp3'}\nhttps://www.youtube.com/watch?v=ok\n",
            encoding="utf-8",
        )
        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["run", "--urls", str(urls_file)])
        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()
        entries = mock_pipeline.call_args[0][0]
        assert len(entries) == 1
        assert entries[0] == "https://www.youtube.com/watch?v=ok"


# ─── pipeline source_type 分岐テスト ──────────────────────────────────────


class TestPipelineLocalBranch:
    def test_local_job_skips_downloader(self, cfg, mp3_file):
        db = cfg.state_db
        init_db(db)
        abs_path = str(mp3_file)

        with (
            patch("transcribe.pipeline.download_audio") as mock_dl,
            patch("transcribe.pipeline.separate_audio", return_value=mp3_file),
            patch("transcribe.pipeline.transcribe", return_value=iter([])),
            patch("transcribe.pipeline.postprocess", return_value=iter([])),
            patch("transcribe.pipeline.compress_repetitions", return_value=iter([])),
            patch("transcribe.pipeline.format_outputs", return_value=0),
        ):
            run_pipeline([abs_path], cfg, GlossaryConfig())

        mock_dl.assert_not_called()

    def test_youtube_job_calls_downloader(self, cfg):
        db = cfg.state_db
        init_db(db)
        url = "https://www.youtube.com/watch?v=TEST1"

        dl_result = MagicMock()
        dl_result.video_id = "TEST1"
        dl_result.title = "Test Video"
        dl_result.audio_path = Path("test.mp3")
        dl_result.upload_date = "20260510"

        with (
            patch("transcribe.pipeline.download_audio", return_value=dl_result) as mock_dl,
            patch("transcribe.pipeline.separate_audio", return_value=Path("test.mp3")),
            patch("transcribe.pipeline.transcribe", return_value=iter([])),
            patch("transcribe.pipeline.postprocess", return_value=iter([])),
            patch("transcribe.pipeline.compress_repetitions", return_value=iter([])),
            patch("transcribe.pipeline.format_outputs", return_value=0),
        ):
            run_pipeline([url], cfg, GlossaryConfig())

        mock_dl.assert_called_once()

    def test_local_job_output_dir_format(self, cfg, mp3_file):
        """出力ディレクトリが {YYYY-MM-DD}_{stem} 形式であること。"""
        db = cfg.state_db
        init_db(db)
        abs_path = str(mp3_file)

        with (
            patch("transcribe.pipeline.separate_audio", return_value=mp3_file),
            patch("transcribe.pipeline.transcribe", return_value=iter([])),
            patch("transcribe.pipeline.postprocess", return_value=iter([])),
            patch("transcribe.pipeline.compress_repetitions", return_value=iter([])),
            patch("transcribe.pipeline.format_outputs", return_value=0) as mock_fmt,
        ):
            run_pipeline([abs_path], cfg, GlossaryConfig())

        call_kwargs = mock_fmt.call_args
        output_dir = call_kwargs.kwargs.get("output_dir") or call_kwargs[1].get("output_dir")
        assert mp3_file.stem in output_dir.name

    def test_local_job_passes_source_type_to_formatter(self, cfg, mp3_file):
        """format_outputs に source_type='local' が渡されること。"""
        db = cfg.state_db
        init_db(db)
        abs_path = str(mp3_file)

        with (
            patch("transcribe.pipeline.separate_audio", return_value=mp3_file),
            patch("transcribe.pipeline.transcribe", return_value=iter([])),
            patch("transcribe.pipeline.postprocess", return_value=iter([])),
            patch("transcribe.pipeline.compress_repetitions", return_value=iter([])),
            patch("transcribe.pipeline.format_outputs", return_value=0) as mock_fmt,
        ):
            run_pipeline([abs_path], cfg, GlossaryConfig())

        call_kwargs = mock_fmt.call_args
        source_type = call_kwargs.kwargs.get("source_type") or call_kwargs[1].get("source_type")
        assert source_type == "local"


# ─── rerun のローカルファイル存在チェック ──────────────────────────────────


class TestRerunLocalFile:
    def test_rerun_missing_local_file_skips(self, db, fake_cfg, tmp_path):
        """source_type=local で元ファイルが存在しない場合は警告して skip。"""
        missing_path = str(tmp_path / "deleted.mp3")
        upsert_job(db, missing_path, source_type="local")
        job = get_job_by_url_or_id(db, missing_path)
        update_status(db, job["id"], "done", video_id="deleted", output_dir=str(tmp_path / "out"))

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["rerun", missing_path, "--yes"])

        assert result.exit_code == 0
        mock_pipeline.assert_not_called()
        assert "ソースファイルが見つかりません" in result.output

    def test_rerun_existing_local_file_runs(self, db, fake_cfg, mp3_file, tmp_path):
        """source_type=local で元ファイルが存在する場合は正常に再実行。"""
        abs_path = str(mp3_file)
        upsert_job(db, abs_path, source_type="local")
        job = get_job_by_url_or_id(db, abs_path)
        update_status(db, job["id"], "done", video_id=mp3_file.stem, output_dir=str(tmp_path / "out"))

        with _mock_env(fake_cfg) as (_, mock_pipeline):
            result = runner.invoke(app, ["rerun", abs_path, "--yes"])

        assert result.exit_code == 0, result.output
        mock_pipeline.assert_called_once()


# ─── NON_RETRYABLE に PermissionError 追加テスト ──────────────────────────


class TestPermissionErrorNonRetryable:
    def test_permission_error_is_non_retryable(self):
        from transcribe.pipeline import is_non_retryable
        assert is_non_retryable(PermissionError("Access denied")) is True

    def test_permission_error_exits_immediately(self, cfg):
        db = cfg.state_db
        init_db(db)
        abs_path = r"C:\locked\file.mp3"
        upsert_job(db, abs_path, source_type="local")

        with (
            patch("transcribe.pipeline._copy_local_file", side_effect=PermissionError("Access denied")),
            patch("transcribe.pipeline.time.sleep") as mock_sleep,
        ):
            run_pipeline([abs_path], cfg, GlossaryConfig())

        mock_sleep.assert_not_called()
