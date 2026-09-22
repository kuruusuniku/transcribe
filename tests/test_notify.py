from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from transcribe.notify import _build_report_body, send_batch_report
from transcribe.config import AppConfig, NotificationConfig, GoogleDocsConfig


# ─── fixtures ─────────────────────────────────────────────────────────────


def _make_cfg(*, enabled: bool = True, to_email: str = "test@example.com", credentials_exist: bool = True, tmp_path: Path = None) -> AppConfig:
    from transcribe.config import (
        PathsConfig, YoutubeConfig, AudioSeparationConfig,
        TranscriptionConfig, OutputConfig, RetryConfig, LoggingConfig,
    )
    cred_path = tmp_path / "credentials.json" if tmp_path else Path("credentials.json")
    token_path = tmp_path / "token.json" if tmp_path else Path("token.json")
    if credentials_exist and tmp_path:
        cred_path.write_text("{}", encoding="utf-8")

    paths = PathsConfig(
        work_dir=tmp_path or Path("data/work"),
        output_dir=tmp_path or Path("data/output"),
        state_db=(tmp_path / "state.db") if tmp_path else Path("data/state.db"),
        log_dir=tmp_path or Path("logs"),
    )
    return AppConfig(
        paths=paths,
        youtube=YoutubeConfig(),
        audio_separation=AudioSeparationConfig(),
        transcription=TranscriptionConfig(),
        output=OutputConfig(),
        retry=RetryConfig(),
        logging=LoggingConfig(),
        google_docs=GoogleDocsConfig(
            credentials_path=cred_path,
            token_path=token_path,
        ),
        notification=NotificationConfig(enabled=enabled, to_email=to_email),
    )


DONE_JOB = {
    "status": "done",
    "title": "テスト動画タイトル",
    "url": "https://www.youtube.com/watch?v=abc123",
    "summarized_at": "2026-06-01 10:00:00",
    "synced_at": None,
    "notion_synced_at": "2026-06-01 11:00:00",
    "error_message": None,
}

FAILED_JOB = {
    "status": "failed",
    "title": None,
    "url": "https://www.youtube.com/watch?v=fail999",
    "summarized_at": None,
    "synced_at": None,
    "notion_synced_at": None,
    "error_message": "DownloadError: HTTP 403\n詳細スタックトレース",
}


# ─── _build_report_body ───────────────────────────────────────────────────


def test_build_report_body_counts():
    body = _build_report_body([DONE_JOB, FAILED_JOB])
    assert "処理結果: 2件 (成功 1 / 失敗 1)" in body


def test_build_report_body_done_job_marks():
    body = _build_report_body([DONE_JOB])
    assert "テスト動画タイトル" in body
    assert "まとめ: ✓" in body
    assert "Docs: —" in body
    assert "Notion: ✓" in body


def test_build_report_body_failed_job_shows_first_line_only():
    body = _build_report_body([FAILED_JOB])
    assert "DownloadError: HTTP 403" in body
    assert "詳細スタックトレース" not in body


def test_build_report_body_failed_job_uses_url_when_no_title():
    body = _build_report_body([FAILED_JOB])
    assert "https://www.youtube.com/watch?v=fail999" in body


def test_build_report_body_failed_no_error_message():
    job = {**FAILED_JOB, "error_message": None}
    body = _build_report_body([job])
    assert "不明なエラー" in body


# ─── send_batch_report ────────────────────────────────────────────────────


def test_send_batch_report_skips_when_disabled(tmp_path):
    cfg = _make_cfg(enabled=False, tmp_path=tmp_path)
    with patch("transcribe.notify.get_credentials") as mock_creds:
        send_batch_report([DONE_JOB], cfg)
        mock_creds.assert_not_called()


def test_send_batch_report_skips_when_no_to_email(tmp_path):
    cfg = _make_cfg(to_email="", tmp_path=tmp_path)
    with patch("transcribe.notify.get_credentials") as mock_creds:
        send_batch_report([DONE_JOB], cfg)
        mock_creds.assert_not_called()


def test_send_batch_report_skips_when_no_credentials(tmp_path):
    cfg = _make_cfg(credentials_exist=False, tmp_path=tmp_path)
    with patch("transcribe.notify.get_credentials") as mock_creds:
        send_batch_report([DONE_JOB], cfg)
        mock_creds.assert_not_called()


def test_send_batch_report_calls_gmail_api(tmp_path):
    cfg = _make_cfg(tmp_path=tmp_path)

    mock_creds = MagicMock()
    mock_service = MagicMock()
    mock_send = mock_service.users.return_value.messages.return_value.send.return_value

    with patch("transcribe.notify.get_credentials", return_value=mock_creds) as mock_get_creds, \
         patch("transcribe.notify.build", return_value=mock_service) as mock_build:
        send_batch_report([DONE_JOB, FAILED_JOB], cfg)

    mock_get_creds.assert_called_once()
    mock_build.assert_called_once_with("gmail", "v1", credentials=mock_creds)
    mock_service.users().messages().send.assert_called_once()

    call_kwargs = mock_service.users().messages().send.call_args
    body_arg = call_kwargs[1]["body"] if call_kwargs[1] else call_kwargs[0][1]
    assert "raw" in body_arg


def test_send_batch_report_subject_contains_counts(tmp_path):
    import base64
    import email
    import email.header

    cfg = _make_cfg(tmp_path=tmp_path)

    mock_creds = MagicMock()
    mock_service = MagicMock()

    with patch("transcribe.notify.get_credentials", return_value=mock_creds), \
         patch("transcribe.notify.build", return_value=mock_service):
        send_batch_report([DONE_JOB, FAILED_JOB], cfg)

    send_call = mock_service.users().messages().send.call_args
    body_raw = send_call.kwargs["body"]["raw"]
    mime_bytes = base64.urlsafe_b64decode(body_raw)
    msg = email.message_from_bytes(mime_bytes)
    subject_parts = email.header.decode_header(msg["subject"])
    decoded_subject = "".join(
        part.decode(enc or "utf-8") if isinstance(part, bytes) else part
        for part, enc in subject_parts
    )
    assert "2件処理" in decoded_subject
    assert "成功1" in decoded_subject
    assert "失敗1" in decoded_subject


def test_send_batch_report_does_not_raise_on_api_error(tmp_path):
    cfg = _make_cfg(tmp_path=tmp_path)

    mock_creds = MagicMock()
    mock_service = MagicMock()
    mock_service.users().messages().send.return_value.execute.side_effect = Exception("API error")

    with patch("transcribe.notify.get_credentials", return_value=mock_creds), \
         patch("transcribe.notify.build", return_value=mock_service):
        send_batch_report([DONE_JOB], cfg)


def test_build_report_body_shows_post_error():
    job = {**DONE_JOB, "post_error": "まとめ生成: timeout\nNotion 同期: 401"}
    body = _build_report_body([job])
    assert "⚠ 後処理失敗: まとめ生成: timeout" in body
    assert "⚠ 後処理失敗: Notion 同期: 401" in body
    assert "後処理が失敗したジョブが 1 件" in body
