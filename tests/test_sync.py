from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

import pytest

from transcribe.config import GoogleDocsConfig
from transcribe.state import (
    get_all_done_jobs,
    get_job_by_id,
    get_unsynced_done_jobs,
    init_db,
    record_synced,
    update_status,
    upsert_job,
)
from transcribe.sync import (
    _extract_title,
    get_credentials,
    get_or_create_subfolder,
    sync_job,
    upload_as_google_doc,
)


# ─── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "state.db"
    init_db(db_path)
    return db_path


@pytest.fixture
def gdocs_cfg(tmp_path):
    creds = tmp_path / "credentials.json"
    creds.write_text("{}", encoding="utf-8")
    return GoogleDocsConfig(
        enabled=True,
        credentials_path=creds,
        token_path=tmp_path / "token.json",
        root_folder_id="root123",
    )


@pytest.fixture
def transcript_dir(tmp_path):
    d = tmp_path / "output" / "2026-05-19_abc123"
    d.mkdir(parents=True)
    (d / "transcript.md").write_text("# テスト動画タイトル\n\n本文", encoding="utf-8")
    return d


# ─── get_credentials ──────────────────────────────────────────────────────


@patch("transcribe.sync.Credentials")
def test_get_credentials_existing_valid_token(mock_creds_cls, tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")
    mock_creds = MagicMock()
    mock_creds.valid = True
    mock_creds_cls.from_authorized_user_file.return_value = mock_creds

    result = get_credentials(tmp_path / "credentials.json", token_path)
    assert result is mock_creds
    mock_creds_cls.from_authorized_user_file.assert_called_once()


@patch("transcribe.sync.InstalledAppFlow")
@patch("transcribe.sync.Credentials")
def test_get_credentials_no_token_runs_flow(mock_creds_cls, mock_flow_cls, tmp_path):
    token_path = tmp_path / "token.json"
    creds_path = tmp_path / "credentials.json"
    creds_path.write_text("{}", encoding="utf-8")

    mock_creds_cls.from_authorized_user_file.side_effect = Exception("no file")

    mock_creds = MagicMock()
    mock_creds.to_json.return_value = '{"token": "test"}'
    mock_flow = MagicMock()
    mock_flow.run_local_server.return_value = mock_creds
    mock_flow_cls.from_client_secrets_file.return_value = mock_flow

    result = get_credentials(creds_path, token_path)
    assert result is mock_creds
    mock_flow.run_local_server.assert_called_once_with(port=0)
    assert token_path.exists()


@patch("transcribe.sync.Request")
@patch("transcribe.sync.Credentials")
def test_get_credentials_expired_token_refreshes(mock_creds_cls, mock_request_cls, tmp_path):
    token_path = tmp_path / "token.json"
    token_path.write_text("{}", encoding="utf-8")

    mock_creds = MagicMock()
    mock_creds.valid = False
    mock_creds.expired = True
    mock_creds.refresh_token = "refresh_tok"
    mock_creds.to_json.return_value = '{"refreshed": true}'
    mock_creds_cls.from_authorized_user_file.return_value = mock_creds

    result = get_credentials(tmp_path / "credentials.json", token_path)
    mock_creds.refresh.assert_called_once()
    assert result is mock_creds


# ─── get_or_create_subfolder ──────────────────────────────────────────────


def test_get_or_create_subfolder_existing():
    service = MagicMock()
    service.files().list().execute.return_value = {"files": [{"id": "folder456"}]}
    result = get_or_create_subfolder(service, "root123", "youtube")
    assert result == "folder456"


def test_get_or_create_subfolder_creates_new():
    service = MagicMock()
    service.files().list().execute.return_value = {"files": []}
    service.files().create().execute.return_value = {"id": "new789"}
    result = get_or_create_subfolder(service, "root123", "local")
    assert result == "new789"


# ─── _extract_title ───────────────────────────────────────────────────────


def test_extract_title_from_heading(tmp_path):
    md = tmp_path / "transcript.md"
    md.write_text("# 素晴らしい動画\n\n本文です", encoding="utf-8")
    assert _extract_title(md) == "素晴らしい動画"


def test_extract_title_fallback_to_stem(tmp_path):
    md = tmp_path / "transcript.md"
    md.write_text("見出しなしの本文", encoding="utf-8")
    assert _extract_title(md) == "transcript"


# ─── upload_as_google_doc ─────────────────────────────────────────────────


@patch("transcribe.sync.MediaFileUpload")
def test_upload_as_google_doc(mock_media_cls, tmp_path):
    service = MagicMock()
    service.files().create().execute.return_value = {"id": "doc_abc"}
    md = tmp_path / "transcript.md"
    md.write_text("# Test", encoding="utf-8")

    result = upload_as_google_doc(service, "folder123", "Test Title", md)
    assert result == "doc_abc"


@patch("transcribe.sync.MediaFileUpload")
def test_upload_as_google_doc_api_error(mock_media_cls, tmp_path):
    service = MagicMock()
    service.files().create().execute.side_effect = Exception("API Error")
    md = tmp_path / "transcript.md"
    md.write_text("# Test", encoding="utf-8")

    with pytest.raises(Exception, match="API Error"):
        upload_as_google_doc(service, "folder123", "Test", md)


# ─── sync_job ─────────────────────────────────────────────────────────────


@patch("transcribe.sync.build")
@patch("transcribe.sync.get_credentials")
def test_sync_job_success(mock_get_creds, mock_build, transcript_dir, gdocs_cfg):
    mock_service = MagicMock()
    mock_build.return_value = mock_service
    mock_service.files().list().execute.return_value = {"files": [{"id": "sub1"}]}
    mock_service.files().create().execute.return_value = {"id": "doc123"}

    job = {"source_type": "youtube"}
    result = sync_job(job, transcript_dir, gdocs_cfg)
    assert result == "doc123"


def test_sync_job_no_transcript(tmp_path, gdocs_cfg):
    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    result = sync_job({"source_type": "youtube"}, empty_dir, gdocs_cfg)
    assert result is None


def test_sync_job_no_credentials(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    (d / "transcript.md").write_text("# test", encoding="utf-8")
    cfg = GoogleDocsConfig(
        enabled=True,
        credentials_path=tmp_path / "nonexistent.json",
        token_path=tmp_path / "token.json",
        root_folder_id="root123",
    )
    with pytest.raises(FileNotFoundError, match="credentials.json"):
        sync_job({"source_type": "youtube"}, d, cfg)


# ─── state.py: synced_at ─────────────────────────────────────────────────


def test_synced_at_migration(db):
    job_id = upsert_job(db, "https://example.com/1")
    update_status(db, job_id, "done", output_dir="/tmp/out")
    job = get_job_by_id(db, job_id)
    assert job["synced_at"] is None


def test_get_unsynced_done_jobs(db):
    j1 = upsert_job(db, "https://example.com/1")
    j2 = upsert_job(db, "https://example.com/2")
    j3 = upsert_job(db, "https://example.com/3")
    update_status(db, j1, "done", output_dir="/tmp/1")
    update_status(db, j2, "done", output_dir="/tmp/2")
    update_status(db, j3, "queued")

    record_synced(db, j1)

    unsynced = get_unsynced_done_jobs(db)
    assert len(unsynced) == 1
    assert unsynced[0]["id"] == j2


def test_record_synced(db):
    job_id = upsert_job(db, "https://example.com/1")
    update_status(db, job_id, "done", output_dir="/tmp/1")
    record_synced(db, job_id)
    job = get_job_by_id(db, job_id)
    assert job["synced_at"] is not None


def test_get_all_done_jobs(db):
    j1 = upsert_job(db, "https://example.com/1")
    j2 = upsert_job(db, "https://example.com/2")
    j3 = upsert_job(db, "https://example.com/3")
    update_status(db, j1, "done", output_dir="/tmp/1")
    update_status(db, j2, "done", output_dir="/tmp/2")
    update_status(db, j3, "failed")

    record_synced(db, j1)

    all_done = get_all_done_jobs(db)
    assert len(all_done) == 2


# ─── CLI sync command ────────────────────────────────────────────────────


def test_cli_sync_disabled():
    from typer.testing import CliRunner
    from transcribe.cli import app
    from transcribe.config import (
        AppConfig,
        AudioSeparationConfig,
        GoogleDocsConfig,
        LoggingConfig,
        OutputConfig,
        PathsConfig,
        RetryConfig,
        TranscriptionConfig,
        YoutubeConfig,
    )

    runner = CliRunner()

    def _fake_load(tmp_path):
        cfg = AppConfig(
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
            retry=RetryConfig(),
            logging=LoggingConfig(),
            google_docs=GoogleDocsConfig(enabled=False),
        )
        return cfg, MagicMock()

    with patch("transcribe.cli._load_cfg_and_glossary", side_effect=lambda: _fake_load(Path("."))):
        result = runner.invoke(app, ["sync"])
    assert "無効" in result.output


def test_cli_sync_no_root_folder():
    from typer.testing import CliRunner
    from transcribe.cli import app
    from transcribe.config import (
        AppConfig,
        AudioSeparationConfig,
        GoogleDocsConfig,
        LoggingConfig,
        OutputConfig,
        PathsConfig,
        RetryConfig,
        TranscriptionConfig,
        YoutubeConfig,
    )

    runner = CliRunner()

    def _fake_load(tmp_path):
        cfg = AppConfig(
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
            retry=RetryConfig(),
            logging=LoggingConfig(),
            google_docs=GoogleDocsConfig(enabled=True, root_folder_id=""),
        )
        return cfg, MagicMock()

    with patch("transcribe.cli._load_cfg_and_glossary", side_effect=lambda: _fake_load(Path("."))):
        result = runner.invoke(app, ["sync"])
    assert "ルートフォルダ" in result.output


# ─── pipeline auto-sync ──────────────────────────────────────────────────


@patch("transcribe.pipeline.sync_job")
@patch("transcribe.pipeline.record_synced")
def test_pipeline_auto_sync_failure_does_not_fail_job(mock_record, mock_sync, db, tmp_path):
    mock_sync.side_effect = Exception("network error")

    j1 = upsert_job(db, "https://example.com/1")
    update_status(db, j1, "done", output_dir=str(tmp_path))

    from transcribe.config import GoogleDocsConfig

    cfg_gd = GoogleDocsConfig(enabled=True, root_folder_id="root123")

    from transcribe.pipeline import logger as pipe_logger

    with patch.object(pipe_logger, "warning") as mock_warn:
        from transcribe.pipeline import sync_job as psync, record_synced as precord

        try:
            psync({"source_type": "youtube"}, tmp_path, cfg_gd)
        except Exception:
            pass

    job = get_job_by_id(db, j1)
    assert job["status"] == "done"
