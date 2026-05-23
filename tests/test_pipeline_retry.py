"""pipeline.py のリトライ抑止ロジックのユニットテスト。

実 state.db / data/output/ には一切触れない。
- DB: tmp_path 上の一時 SQLite
- ネットワーク/ダウンロード/文字起こし: unittest.mock でモック
- time.sleep: unittest.mock でモック（実際の待機は発生しない）
"""
from __future__ import annotations

from unittest.mock import MagicMock, call, patch

import pytest
import yt_dlp.utils

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
from transcribe.pipeline import is_non_retryable, run_pipeline
from transcribe.state import init_db, upsert_job


# ─── fixtures ─────────────────────────────────────────────────────────────────


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
def db(cfg):
    init_db(cfg.state_db)
    return cfg.state_db


# ─── is_non_retryable() 単体テスト ────────────────────────────────────────────


class TestIsNonRetryable:
    def test_module_not_found_error(self):
        assert is_non_retryable(ModuleNotFoundError("test")) is True

    def test_import_error(self):
        assert is_non_retryable(ImportError("test")) is True

    def test_unsupported_url_error(self):
        assert is_non_retryable(yt_dlp.utils.UnsupportedError("https://example.com/bad")) is True

    def test_file_not_found_error(self):
        assert is_non_retryable(FileNotFoundError("test")) is True

    def test_key_error(self):
        assert is_non_retryable(KeyError("test")) is False

    def test_attribute_error(self):
        assert is_non_retryable(AttributeError("test")) is False

    def test_type_error(self):
        assert is_non_retryable(TypeError("test")) is False

    def test_runtime_error_torchaudio_backend(self):
        assert is_non_retryable(RuntimeError("Couldn't find appropriate backend for uri")) is True

    def test_runtime_error_cuda_oom(self):
        assert is_non_retryable(RuntimeError("CUDA out of memory at layer 3")) is True

    def test_runtime_error_no_such_file(self):
        assert is_non_retryable(RuntimeError("No such file or directory: /tmp/foo")) is True

    def test_runtime_error_permission_denied(self):
        assert is_non_retryable(RuntimeError("Permission denied: /var/secret")) is True

    def test_runtime_error_generic_is_retryable(self):
        assert is_non_retryable(RuntimeError("一般的なエラー")) is False

    def test_connection_error_is_retryable(self):
        assert is_non_retryable(ConnectionError("ネットワーク不調")) is False

    def test_base_exception_is_retryable(self):
        assert is_non_retryable(Exception("汎用例外")) is False

    def test_value_error_is_retryable(self):
        assert is_non_retryable(ValueError("値エラー")) is False


# ─── run_pipeline() リトライ挙動テスト ───────────────────────────────────────


class TestRunPipelineRetry:
    def test_non_retryable_exits_immediately(self, cfg, db):
        """NON_RETRYABLE 例外は 1 回目で即 break し、リトライ 0 回で終了する。"""
        url = "https://www.youtube.com/watch?v=BAD_MODULE"
        upsert_job(db, url)

        with (
            patch("transcribe.pipeline.download_audio", side_effect=ModuleNotFoundError("No module named 'demucs.api'")) as mock_dl,
            patch("transcribe.pipeline.time.sleep") as mock_sleep,
        ):
            run_pipeline([url], cfg, GlossaryConfig())

        assert mock_dl.call_count == 1
        mock_sleep.assert_not_called()

    def test_transient_failure_retries_max_attempts(self, cfg, db):
        """一時的な失敗（ConnectionError）は max_attempts 回試行し、
        その間に (max_attempts - 1) 回 time.sleep が呼ばれる。"""
        url = "https://www.youtube.com/watch?v=NET_FAIL"
        upsert_job(db, url)

        with (
            patch("transcribe.pipeline.download_audio", side_effect=ConnectionError("ネットワーク不調")) as mock_dl,
            patch("transcribe.pipeline.time.sleep") as mock_sleep,
        ):
            run_pipeline([url], cfg, GlossaryConfig())

        assert mock_dl.call_count == 3
        assert mock_sleep.call_count == 2

    def test_success_on_second_attempt(self, cfg, db):
        """1 回目が一時的失敗、2 回目が成功するシナリオ。

        _run_job 全体をモックしている理由:
            成功ケースでは _run_job 内で download_audio → separate_audio →
            transcribe → format_outputs と多段の関数が連鎖するため、
            それら全てをモックすると複雑になりすぎる。
            _run_job の内部実装には依存せず、run_pipeline のリトライ制御
            （2 回目で break して success=True になる）だけを検証する。
        """
        url = "https://www.youtube.com/watch?v=RETRY_OK"
        upsert_job(db, url)

        with (
            patch(
                "transcribe.pipeline._run_job",
                side_effect=[ConnectionError("1回目: 一時的なネットワーク失敗"), None],
            ) as mock_run_job,
            patch("transcribe.pipeline.time.sleep") as mock_sleep,
        ):
            run_pipeline([url], cfg, GlossaryConfig())

        assert mock_run_job.call_count == 2
        assert mock_sleep.call_count == 1

    def test_non_retryable_does_not_call_sleep(self, cfg, db):
        """NON_RETRYABLE エラー検出時は time.sleep が一切呼ばれないことを確認。

        test_non_retryable_exits_immediately と別角度で検証:
        yt_dlp.utils.UnsupportedError（実運用での典型ケース）で確認する。
        """
        url = "https://invalid-site.example/watch?v=xxx"
        upsert_job(db, url)

        with (
            patch(
                "transcribe.pipeline.download_audio",
                side_effect=yt_dlp.utils.UnsupportedError("https://invalid-site.example/watch?v=xxx"),
            ),
            patch("transcribe.pipeline.time.sleep") as mock_sleep,
        ):
            run_pipeline([url], cfg, GlossaryConfig())

        mock_sleep.assert_not_called()
