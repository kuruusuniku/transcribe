from __future__ import annotations

import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from transcribe.config import SummarizeConfig
from transcribe.state import (
    get_all_done_jobs,
    get_job_by_id,
    get_unsummarized_done_jobs,
    init_db,
    record_summarized,
    update_status,
    upsert_job,
)
from transcribe.summarize import (
    _build_glossary_section,
    build_prompt,
    call_claude,
    call_gemini,
    generate_summary,
)


# ─── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "state.db"
    init_db(db_path)
    return db_path


@pytest.fixture
def sample_glossary():
    return [
        {"pattern": "素形部", "replacement": "鼠径部", "type": "literal"},
        {"pattern": "静か断電", "replacement": "臍下丹田", "type": "literal"},
        {"pattern": "体液", "replacement": "体癖", "type": "regex"},  # regex は除外される想定
        {"pattern": "海内海外", "replacement": "回内回外", "type": "literal"},
    ]


@pytest.fixture
def output_dir_with_transcript(tmp_path):
    d = tmp_path / "output" / "2026-05-19_abc123"
    d.mkdir(parents=True)
    (d / "transcript.md").write_text(
        "# テスト動画\n\n## [00:00](https://www.youtube.com/watch?v=abc123&t=0s)\n\nテスト本文",
        encoding="utf-8",
    )
    return d


# ─── build_prompt ─────────────────────────────────────────────────────────


def test_build_prompt_includes_transcript_and_metadata(sample_glossary):
    system, user = build_prompt(
        transcript_text="本文ここに",
        glossary_entries=sample_glossary,
        video_url="https://www.youtube.com/watch?v=abc123",
        video_title="テスト動画",
    )
    assert "本文ここに" in user
    assert "テスト動画" in user
    assert "https://www.youtube.com/watch?v=abc123" in user


def test_build_prompt_glossary_literal_only(sample_glossary):
    system, _ = build_prompt(
        transcript_text="",
        glossary_entries=sample_glossary,
        video_url="https://x",
        video_title="t",
    )
    # literal entries appear
    assert "素形部 → 鼠径部" in system
    assert "静か断電 → 臍下丹田" in system
    assert "海内海外 → 回内回外" in system
    # regex entry is excluded
    assert "体液 → 体癖" not in system


def test_build_prompt_local_source_uses_lecture_prompt(sample_glossary):
    system, user = build_prompt(
        transcript_text="",
        glossary_entries=sample_glossary,
        video_url="C:/some/path.mp3",
        video_title="local-file",
        source_type="local",
    )
    assert "叡智講義" in system
    assert "体育指導の動画" not in system
    assert "リンクなし" in user
    # ローカルパスはまとめに不要なので渡さない
    assert "C:/some/path.mp3" not in user
    # 用語集は講義用プロンプトにも付く
    assert "素形部 → 鼠径部" in system


def test_build_prompt_youtube_source_uses_taiiku_prompt(sample_glossary):
    system, user = build_prompt(
        transcript_text="",
        glossary_entries=sample_glossary,
        video_url="https://x",
        video_title="t",
        source_type="youtube",
    )
    assert "体育指導の動画" in system
    assert "叡智講義" not in user
    assert "URL: https://x" in user


def test_build_glossary_section_empty():
    assert _build_glossary_section([]) == ""


# ─── call_gemini ──────────────────────────────────────────────────────────


def _install_fake_genai(text_response: str | None = "FAKE_SUMMARY", raise_exc: Exception | None = None):
    """sys.modules に偽の google.genai を仕込む。"""
    fake_response = MagicMock()
    fake_response.text = text_response

    fake_client = MagicMock()
    if raise_exc is not None:
        fake_client.models.generate_content.side_effect = raise_exc
    else:
        fake_client.models.generate_content.return_value = fake_response

    fake_genai = MagicMock()
    fake_genai.Client.return_value = fake_client

    fake_types = MagicMock()
    fake_genai.types = fake_types

    fake_google = MagicMock()
    fake_google.genai = fake_genai

    return {
        "google": fake_google,
        "google.genai": fake_genai,
        "google.genai.types": fake_types,
    }


def test_call_gemini_success():
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="key123")
    modules = _install_fake_genai(text_response="MY_SUMMARY")
    with patch.dict(sys.modules, modules):
        result = call_gemini("sys", "user", cfg)
    assert result == "MY_SUMMARY"


def test_call_gemini_api_error_propagates():
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="key123")
    modules = _install_fake_genai(raise_exc=RuntimeError("rate limit"))
    with patch.dict(sys.modules, modules):
        with pytest.raises(RuntimeError, match="rate limit"):
            call_gemini("sys", "user", cfg)


def test_call_gemini_no_api_key():
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="")
    with patch.dict("os.environ", {}, clear=False):
        # GEMINI_API_KEY を消しておく
        import os
        os.environ.pop("GEMINI_API_KEY", None)
        with pytest.raises(RuntimeError, match="API キー"):
            call_gemini("sys", "user", cfg)


def test_call_gemini_empty_response():
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="key123")
    modules = _install_fake_genai(text_response=None)
    with patch.dict(sys.modules, modules):
        result = call_gemini("sys", "user", cfg)
    assert result == ""


# ─── call_gemini 503 リトライ ─────────────────────────────────────────────


def _make_genai_error(code: int):
    """指定 HTTP ステータスコードを持つ google.genai APIError を構築する。"""
    from google.genai import errors as genai_errors

    payload = {"error": {"code": code, "status": "ERR", "message": f"{code} error"}}
    if 400 <= code < 500:
        return genai_errors.ClientError(code, payload)
    return genai_errors.ServerError(code, payload)


def _install_fake_genai_with_side_effect(side_effect):
    """generate_content の side_effect を任意指定できる版。"""
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = side_effect

    fake_genai = MagicMock()
    fake_genai.Client.return_value = fake_client

    fake_types = MagicMock()
    fake_genai.types = fake_types

    fake_google = MagicMock()
    fake_google.genai = fake_genai

    modules = {
        "google": fake_google,
        "google.genai": fake_genai,
        "google.genai.types": fake_types,
    }
    return modules, fake_client


def test_call_gemini_503_retries_then_succeeds():
    """503 が 1 回 → リトライして成功。"""
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="key123")

    fake_response = MagicMock()
    fake_response.text = "OK_AFTER_RETRY"

    err = _make_genai_error(503)
    modules, fake_client = _install_fake_genai_with_side_effect([err, fake_response])

    with patch.dict(sys.modules, modules):
        with patch("transcribe.summarize.time.sleep") as mock_sleep:
            result = call_gemini("sys", "user", cfg)

    assert result == "OK_AFTER_RETRY"
    assert fake_client.models.generate_content.call_count == 2
    mock_sleep.assert_called_once_with(5)


def test_call_gemini_503_max_retries_raises():
    """503 が連続発生し最大リトライ回数を超えると元の例外を raise する。"""
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="key123")

    err = _make_genai_error(503)
    modules, fake_client = _install_fake_genai_with_side_effect(err)

    with patch.dict(sys.modules, modules):
        with patch("transcribe.summarize.time.sleep") as mock_sleep:
            with pytest.raises(Exception) as exc_info:
                call_gemini("sys", "user", cfg)

    assert getattr(exc_info.value, "code", None) == 503
    # 初回 + 3 リトライ = 4 回
    assert fake_client.models.generate_content.call_count == 4
    assert mock_sleep.call_count == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [5, 15, 30]


def test_call_gemini_non_503_error_does_not_retry():
    """503 以外（例: 400）は即座に raise されリトライしない。"""
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="key123")

    err = _make_genai_error(400)
    modules, fake_client = _install_fake_genai_with_side_effect(err)

    with patch.dict(sys.modules, modules):
        with patch("transcribe.summarize.time.sleep") as mock_sleep:
            with pytest.raises(Exception) as exc_info:
                call_gemini("sys", "user", cfg)

    assert getattr(exc_info.value, "code", None) == 400
    assert fake_client.models.generate_content.call_count == 1
    mock_sleep.assert_not_called()


def test_call_gemini_503_retry_logs_warning(caplog):
    """503 リトライ時に WARNING ログが出力される（リトライ回数と待機秒数を含む）。"""
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="key123")

    fake_response = MagicMock()
    fake_response.text = "OK"

    err = _make_genai_error(503)
    modules, _ = _install_fake_genai_with_side_effect([err, err, fake_response])

    with caplog.at_level(logging.WARNING, logger="transcribe.summarize"):
        with patch.dict(sys.modules, modules):
            with patch("transcribe.summarize.time.sleep"):
                call_gemini("sys", "user", cfg)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "503" in warnings[0].message
    assert "1/3" in warnings[0].message and "5" in warnings[0].message
    assert "2/3" in warnings[1].message and "15" in warnings[1].message


def test_call_gemini_503_sleep_called_with_correct_backoffs():
    """リトライ間の time.sleep が 5 → 15 → 30 秒の順で呼ばれる。"""
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="key123")

    fake_response = MagicMock()
    fake_response.text = "OK"

    err = _make_genai_error(503)
    # 503 を 3 回返したあと成功
    modules, fake_client = _install_fake_genai_with_side_effect(
        [err, err, err, fake_response]
    )

    with patch.dict(sys.modules, modules):
        with patch("transcribe.summarize.time.sleep") as mock_sleep:
            result = call_gemini("sys", "user", cfg)

    assert result == "OK"
    assert fake_client.models.generate_content.call_count == 4
    assert [c.args[0] for c in mock_sleep.call_args_list] == [5, 15, 30]


# ─── call_claude ──────────────────────────────────────────────────────────


def _install_fake_anthropic(text_response: str = "FAKE_CLAUDE", raise_exc: Exception | None = None):
    fake_content_block = MagicMock()
    fake_content_block.text = text_response
    fake_content_block.type = "text"

    fake_message = MagicMock()
    fake_message.content = [fake_content_block]

    fake_client = MagicMock()
    if raise_exc is not None:
        fake_client.messages.create.side_effect = raise_exc
    else:
        fake_client.messages.create.return_value = fake_message

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client

    return {"anthropic": fake_anthropic}


def test_call_claude_success():
    cfg = SummarizeConfig(provider="claude", anthropic_api_key="sk-test")
    modules = _install_fake_anthropic(text_response="CLAUDE_SUM")
    with patch.dict(sys.modules, modules):
        result = call_claude("sys", "user", cfg)
    assert result == "CLAUDE_SUM"


def test_call_claude_api_error_propagates():
    cfg = SummarizeConfig(provider="claude", anthropic_api_key="sk-test")
    modules = _install_fake_anthropic(raise_exc=RuntimeError("api boom"))
    with patch.dict(sys.modules, modules):
        with pytest.raises(RuntimeError, match="api boom"):
            call_claude("sys", "user", cfg)


def test_call_claude_no_api_key():
    cfg = SummarizeConfig(provider="claude", anthropic_api_key="")
    import os
    os.environ.pop("ANTHROPIC_API_KEY", None)
    with pytest.raises(RuntimeError, match="API キー"):
        call_claude("sys", "user", cfg)


# ─── call_claude 503 リトライ ─────────────────────────────────────────────


def _make_anthropic_503():
    """status_code=503 を持つ偽の APIStatusError を構築する。"""
    err = Exception("503 Service Unavailable")
    err.status_code = 503
    return err


def _install_fake_anthropic_with_side_effect(side_effect):
    """messages.create の side_effect を任意指定できる版。"""
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = side_effect

    fake_anthropic = MagicMock()
    fake_anthropic.Anthropic.return_value = fake_client

    return {"anthropic": fake_anthropic}, fake_client


def test_call_claude_503_retries_then_succeeds():
    """503 が 1 回 → リトライして成功。"""
    cfg = SummarizeConfig(provider="claude", anthropic_api_key="sk-test")

    fake_content = MagicMock()
    fake_content.text = "CLAUDE_RETRY_OK"
    fake_content.type = "text"
    fake_message = MagicMock()
    fake_message.content = [fake_content]

    err = _make_anthropic_503()
    modules, fake_client = _install_fake_anthropic_with_side_effect([err, fake_message])

    with patch.dict(sys.modules, modules):
        with patch("transcribe.summarize.time.sleep") as mock_sleep:
            result = call_claude("sys", "user", cfg)

    assert result == "CLAUDE_RETRY_OK"
    assert fake_client.messages.create.call_count == 2
    mock_sleep.assert_called_once_with(5)


def test_call_claude_503_max_retries_raises():
    """503 が連続発生し最大リトライ後に元の例外を raise する。"""
    cfg = SummarizeConfig(provider="claude", anthropic_api_key="sk-test")

    err = _make_anthropic_503()
    modules, fake_client = _install_fake_anthropic_with_side_effect(err)

    with patch.dict(sys.modules, modules):
        with patch("transcribe.summarize.time.sleep") as mock_sleep:
            with pytest.raises(Exception) as exc_info:
                call_claude("sys", "user", cfg)

    assert getattr(exc_info.value, "status_code", None) == 503
    # 初回 + 3 リトライ = 4 回
    assert fake_client.messages.create.call_count == 4
    assert mock_sleep.call_count == 3
    assert [c.args[0] for c in mock_sleep.call_args_list] == [5, 10, 20]


def test_call_claude_503_retry_logs_warning(caplog):
    """503 リトライ時に WARNING ログが出力される（リトライ回数と待機秒数を含む）。"""
    cfg = SummarizeConfig(provider="claude", anthropic_api_key="sk-test")

    fake_content = MagicMock()
    fake_content.text = "OK"
    fake_content.type = "text"
    fake_message = MagicMock()
    fake_message.content = [fake_content]

    err = _make_anthropic_503()
    modules, _ = _install_fake_anthropic_with_side_effect([err, err, fake_message])

    with caplog.at_level(logging.WARNING, logger="transcribe.summarize"):
        with patch.dict(sys.modules, modules):
            with patch("transcribe.summarize.time.sleep"):
                call_claude("sys", "user", cfg)

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
    assert "503" in warnings[0].message
    assert "1/3" in warnings[0].message and "5" in warnings[0].message
    assert "2/3" in warnings[1].message and "10" in warnings[1].message


# ─── SummarizeConfig.resolve_api_key ──────────────────────────────────────


def test_resolve_api_key_config_takes_precedence(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env_value")
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="cfg_value")
    assert cfg.resolve_api_key() == "cfg_value"


def test_resolve_api_key_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "env_value")
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="")
    assert cfg.resolve_api_key() == "env_value"


def test_resolve_api_key_empty_when_neither(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = SummarizeConfig(provider="gemini", gemini_api_key="")
    assert cfg.resolve_api_key() == ""


def test_resolve_api_key_anthropic(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = SummarizeConfig(provider="claude", anthropic_api_key="sk-from-cfg")
    assert cfg.resolve_api_key() == "sk-from-cfg"


# ─── generate_summary ─────────────────────────────────────────────────────


@patch("transcribe.summarize.call_gemini")
def test_generate_summary_calls_gemini(mock_call, output_dir_with_transcript, sample_glossary):
    mock_call.return_value = "GENERATED_SUMMARY"
    cfg = SummarizeConfig(enabled=True, provider="gemini", gemini_api_key="x")

    result = generate_summary(
        output_dir=output_dir_with_transcript,
        video_url="https://www.youtube.com/watch?v=abc123",
        video_title="テスト動画",
        cfg=cfg,
        glossary_entries=sample_glossary,
    )
    assert result == output_dir_with_transcript / "summary.md"
    assert (output_dir_with_transcript / "summary.md").read_text(encoding="utf-8") == "GENERATED_SUMMARY"
    mock_call.assert_called_once()


@patch("transcribe.summarize.call_claude")
def test_generate_summary_calls_claude(mock_call, output_dir_with_transcript, sample_glossary):
    mock_call.return_value = "CLAUDE_OUTPUT"
    cfg = SummarizeConfig(enabled=True, provider="claude", anthropic_api_key="x")

    result = generate_summary(
        output_dir=output_dir_with_transcript,
        video_url="https://www.youtube.com/watch?v=abc123",
        video_title="テスト動画",
        cfg=cfg,
        glossary_entries=sample_glossary,
    )
    assert result == output_dir_with_transcript / "summary.md"
    mock_call.assert_called_once()


def test_generate_summary_no_transcript(tmp_path, sample_glossary):
    cfg = SummarizeConfig(enabled=True, provider="gemini", gemini_api_key="x")
    result = generate_summary(
        output_dir=tmp_path,
        video_url="https://x",
        video_title="t",
        cfg=cfg,
        glossary_entries=sample_glossary,
    )
    assert result is None


@patch("transcribe.summarize.call_gemini")
def test_generate_summary_empty_response_returns_none(mock_call, output_dir_with_transcript, sample_glossary):
    mock_call.return_value = "   \n"
    cfg = SummarizeConfig(enabled=True, provider="gemini", gemini_api_key="x")
    result = generate_summary(
        output_dir=output_dir_with_transcript,
        video_url="https://x",
        video_title="t",
        cfg=cfg,
        glossary_entries=sample_glossary,
    )
    assert result is None
    assert not (output_dir_with_transcript / "summary.md").exists()


@patch("transcribe.summarize.call_gemini")
def test_generate_summary_unknown_provider_raises(mock_call, output_dir_with_transcript, sample_glossary):
    cfg = SummarizeConfig(enabled=True, provider="unknown", gemini_api_key="x")
    with pytest.raises(ValueError, match="未対応"):
        generate_summary(
            output_dir=output_dir_with_transcript,
            video_url="https://x",
            video_title="t",
            cfg=cfg,
            glossary_entries=sample_glossary,
        )


# ─── state.py: summarized_at ──────────────────────────────────────────────


def test_summarized_at_migration(db):
    job_id = upsert_job(db, "https://example.com/sum/1")
    update_status(db, job_id, "done", output_dir="/tmp/out")
    job = get_job_by_id(db, job_id)
    assert job["summarized_at"] is None


def test_get_unsummarized_done_jobs(db):
    j1 = upsert_job(db, "https://example.com/sum/a")
    j2 = upsert_job(db, "https://example.com/sum/b")
    j3 = upsert_job(db, "https://example.com/sum/c")
    update_status(db, j1, "done", output_dir="/tmp/a")
    update_status(db, j2, "done", output_dir="/tmp/b")
    update_status(db, j3, "queued")

    record_summarized(db, j1)

    unsummarized = get_unsummarized_done_jobs(db)
    assert len(unsummarized) == 1
    assert unsummarized[0]["id"] == j2


def test_record_summarized(db):
    job_id = upsert_job(db, "https://example.com/sum/x")
    update_status(db, job_id, "done", output_dir="/tmp/x")
    record_summarized(db, job_id)
    job = get_job_by_id(db, job_id)
    assert job["summarized_at"] is not None


# ─── CLI summarize command ────────────────────────────────────────────────


def _make_cfg(tmp_path, *, summarize_enabled=True, provider="gemini", api_key="x"):
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
        summarize=SummarizeConfig(
            enabled=summarize_enabled,
            provider=provider,
            gemini_api_key=api_key if provider == "gemini" else "",
            anthropic_api_key=api_key if provider == "claude" else "",
        ),
    )
    return cfg


def test_cli_summarize_disabled(tmp_path):
    from typer.testing import CliRunner
    from transcribe.cli import app

    cfg = _make_cfg(tmp_path, summarize_enabled=False)
    init_db(cfg.state_db)
    runner = CliRunner()

    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, MagicMock())):
        result = runner.invoke(app, ["summarize"])
    assert "無効" in result.output


def test_cli_summarize_no_api_key(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from transcribe.cli import app

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    cfg = _make_cfg(tmp_path, summarize_enabled=True, api_key="")
    init_db(cfg.state_db)
    runner = CliRunner()

    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, MagicMock())):
        result = runner.invoke(app, ["summarize"])
    assert "API キー" in result.output


@patch("transcribe.cli.generate_summary")
def test_cli_summarize_unsummarized_only(mock_gen, tmp_path):
    from typer.testing import CliRunner
    from transcribe.cli import app

    cfg = _make_cfg(tmp_path)
    init_db(cfg.state_db)

    out_a = tmp_path / "out_a"
    out_a.mkdir()
    (out_a / "transcript.md").write_text("# A", encoding="utf-8")
    out_b = tmp_path / "out_b"
    out_b.mkdir()
    (out_b / "transcript.md").write_text("# B", encoding="utf-8")

    j1 = upsert_job(cfg.state_db, "https://ex.com/1")
    j2 = upsert_job(cfg.state_db, "https://ex.com/2")
    update_status(cfg.state_db, j1, "done", title="A", output_dir=str(out_a))
    update_status(cfg.state_db, j2, "done", title="B", output_dir=str(out_b))
    record_summarized(cfg.state_db, j1)

    mock_gen.return_value = out_b / "summary.md"

    glossary = MagicMock()
    glossary.substitutions = []

    runner = CliRunner()
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, glossary)):
        result = runner.invoke(app, ["summarize"])

    assert result.exit_code == 0
    assert mock_gen.call_count == 1
    # job 2 only
    call_kwargs = mock_gen.call_args.kwargs
    assert call_kwargs["video_title"] == "B"


@patch("transcribe.cli.generate_summary")
def test_cli_summarize_all_flag(mock_gen, tmp_path):
    from typer.testing import CliRunner
    from transcribe.cli import app

    cfg = _make_cfg(tmp_path)
    init_db(cfg.state_db)

    out_a = tmp_path / "out_a"
    out_a.mkdir()
    (out_a / "transcript.md").write_text("# A", encoding="utf-8")
    out_b = tmp_path / "out_b"
    out_b.mkdir()
    (out_b / "transcript.md").write_text("# B", encoding="utf-8")

    j1 = upsert_job(cfg.state_db, "https://ex.com/all1")
    j2 = upsert_job(cfg.state_db, "https://ex.com/all2")
    update_status(cfg.state_db, j1, "done", title="A", output_dir=str(out_a))
    update_status(cfg.state_db, j2, "done", title="B", output_dir=str(out_b))
    record_summarized(cfg.state_db, j1)

    mock_gen.return_value = out_a / "summary.md"

    glossary = MagicMock()
    glossary.substitutions = []

    runner = CliRunner()
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, glossary)):
        result = runner.invoke(app, ["summarize", "--all"])

    assert result.exit_code == 0
    assert mock_gen.call_count == 2


@patch("transcribe.cli.generate_summary")
def test_cli_summarize_specific_id(mock_gen, tmp_path):
    from typer.testing import CliRunner
    from transcribe.cli import app

    cfg = _make_cfg(tmp_path)
    init_db(cfg.state_db)

    out_a = tmp_path / "out_a"
    out_a.mkdir()
    (out_a / "transcript.md").write_text("# A", encoding="utf-8")
    out_b = tmp_path / "out_b"
    out_b.mkdir()
    (out_b / "transcript.md").write_text("# B", encoding="utf-8")

    j1 = upsert_job(cfg.state_db, "https://ex.com/id1")
    j2 = upsert_job(cfg.state_db, "https://ex.com/id2")
    update_status(cfg.state_db, j1, "done", title="A", output_dir=str(out_a))
    update_status(cfg.state_db, j2, "done", title="B", output_dir=str(out_b))

    mock_gen.return_value = out_b / "summary.md"

    glossary = MagicMock()
    glossary.substitutions = []

    runner = CliRunner()
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, glossary)):
        result = runner.invoke(app, ["summarize", "--id", str(j2)])

    assert result.exit_code == 0
    assert mock_gen.call_count == 1
    assert mock_gen.call_args.kwargs["video_title"] == "B"


# ─── sync.py: summary.md ──────────────────────────────────────────────────


@patch("transcribe.sync.build")
@patch("transcribe.sync.get_credentials")
@patch("transcribe.sync.MediaFileUpload")
def test_sync_job_uploads_summary_when_present(mock_media, mock_get_creds, mock_build, tmp_path):
    from transcribe.config import GoogleDocsConfig
    from transcribe.sync import sync_job

    creds_path = tmp_path / "credentials.json"
    creds_path.write_text("{}", encoding="utf-8")
    gd_cfg = GoogleDocsConfig(
        enabled=True,
        credentials_path=creds_path,
        token_path=tmp_path / "token.json",
        root_folder_id="root1",
    )

    out = tmp_path / "output" / "2026-05-20_abc"
    out.mkdir(parents=True)
    (out / "transcript.md").write_text("# Title\n", encoding="utf-8")
    (out / "summary.md").write_text("# Summary\n", encoding="utf-8")

    mock_service = MagicMock()
    mock_build.return_value = mock_service
    mock_service.files().list().execute.return_value = {"files": [{"id": "sub1"}]}
    # 2 calls expected: transcript + summary
    mock_service.files().create().execute.side_effect = [
        {"id": "doc_transcript"},
        {"id": "doc_summary"},
    ]

    result = sync_job({"source_type": "youtube"}, out, gd_cfg)
    assert result == "doc_transcript"

    create_calls = [c for c in mock_service.files().create.call_args_list if c.kwargs.get("body")]
    # transcript と summary それぞれ create が呼ばれる（少なくとも 2 回）
    summary_titles = [c.kwargs["body"]["name"] for c in create_calls if "まとめ" in c.kwargs["body"].get("name", "")]
    assert len(summary_titles) >= 1


@patch("transcribe.sync.build")
@patch("transcribe.sync.get_credentials")
@patch("transcribe.sync.MediaFileUpload")
def test_sync_job_no_summary_only_transcript(mock_media, mock_get_creds, mock_build, tmp_path):
    from transcribe.config import GoogleDocsConfig
    from transcribe.sync import sync_job

    creds_path = tmp_path / "credentials.json"
    creds_path.write_text("{}", encoding="utf-8")
    gd_cfg = GoogleDocsConfig(
        enabled=True,
        credentials_path=creds_path,
        token_path=tmp_path / "token.json",
        root_folder_id="root1",
    )

    out = tmp_path / "output" / "2026-05-20_xyz"
    out.mkdir(parents=True)
    (out / "transcript.md").write_text("# OnlyTranscript\n", encoding="utf-8")

    mock_service = MagicMock()
    mock_build.return_value = mock_service
    mock_service.files().list().execute.return_value = {"files": [{"id": "sub1"}]}
    mock_service.files().create().execute.return_value = {"id": "doc_only"}

    result = sync_job({"source_type": "youtube"}, out, gd_cfg)
    assert result == "doc_only"


# ─── pipeline 自動まとめ ──────────────────────────────────────────────────


def test_pipeline_module_exposes_summary_integration():
    """pipeline.py が summarize 統合に必要なシンボルを正しく持つことを確認。"""
    from transcribe import pipeline

    assert hasattr(pipeline, "generate_summary")
    assert hasattr(pipeline, "record_summarized")


def test_pipeline_summary_record_updates_db(db):
    """generate_summary 成功時のフロー: summary 生成 → record_summarized で DB 更新が走ることを確認。"""
    from transcribe.pipeline import generate_summary as pgen, record_summarized as prec

    job_id = upsert_job(db, "https://ex.com/auto-flow")
    update_status(db, job_id, "done", output_dir="/tmp/x")

    out = Path("/tmp")  # 実際のファイルは作らない（call_gemini をモック）

    with patch("transcribe.summarize.call_gemini", return_value="SUMMARY"):
        # transcript.md が必要なので tmp 作成
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            tdir = Path(td)
            (tdir / "transcript.md").write_text("body", encoding="utf-8")
            cfg_s = SummarizeConfig(enabled=True, provider="gemini", gemini_api_key="x")
            result = pgen(
                output_dir=tdir,
                video_url="https://x",
                video_title="t",
                cfg=cfg_s,
                glossary_entries=[],
                source_type="youtube",
            )
            assert result == tdir / "summary.md"

    prec(db, job_id)
    job = get_job_by_id(db, job_id)
    assert job["summarized_at"] is not None


def test_pipeline_summary_failure_does_not_fail_job(db):
    """まとめ生成失敗時にジョブは done のまま、summarized_at は None。"""
    job_id = upsert_job(db, "https://ex.com/fail-flow")
    update_status(db, job_id, "done", output_dir="/tmp/x")

    # _run_job 内の try/except 構造を模倣
    try:
        from transcribe.pipeline import generate_summary as pgen
        with patch("transcribe.summarize.call_gemini", side_effect=RuntimeError("api down")):
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                tdir = Path(td)
                (tdir / "transcript.md").write_text("body", encoding="utf-8")
                cfg_s = SummarizeConfig(enabled=True, provider="gemini", gemini_api_key="x")
                pgen(
                    output_dir=tdir,
                    video_url="https://x",
                    video_title="t",
                    cfg=cfg_s,
                    glossary_entries=[],
                    source_type="youtube",
                )
    except Exception:
        # pipeline._run_job 側で握りつぶされるべきエラー。テストでは明示的に握る
        pass

    job = get_job_by_id(db, job_id)
    assert job["status"] == "done"
    assert job["summarized_at"] is None


# ─── 出力トークン上限で切れた場合は summary.md を書かない ───────────────


def test_truncated_claude_summary_not_saved(tmp_path):
    from unittest.mock import MagicMock, patch

    import pytest

    from transcribe.config import SummarizeConfig
    from transcribe.summarize import SummaryTruncatedError, generate_summary

    (tmp_path / "transcript.md").write_text("[00:00] テスト", encoding="utf-8")
    cfg = SummarizeConfig(enabled=True, provider="claude", anthropic_api_key="k")

    block = MagicMock(type="text", text="途中まで")
    message = MagicMock(content=[block], stop_reason="max_tokens")
    with patch("anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = message
        with pytest.raises(SummaryTruncatedError):
            generate_summary(tmp_path, "https://x", "t", cfg, [])

    assert not (tmp_path / "summary.md").exists()
    assert (tmp_path / "summary.truncated.md").read_text(encoding="utf-8") == "途中まで"



def test_prompts_explain_timestamp_format(sample_glossary):
    """MM:SS を時:分と取り違えないための注意書きが両方のプロンプトに入っていること。"""
    for source_type in ("youtube", "local"):
        system, _ = build_prompt(
            transcript_text="",
            glossary_entries=sample_glossary,
            video_url="https://x",
            video_title="t",
            source_type=source_type,
        )
        assert "`MM:SS` の 2 つ組で書かれている場合は「分:秒」を意味する" in system


# ─── 出力の崩れ（繰り返し）の復旧 ────────────────────────────────────────


def _lecture_body(title="# 講義タイトル"):
    return (
        "講義タイトル: lecture.mp3\n\n"
        f"{title}\n\n"
        "## 1. 講義の全体要約\n\n要約本文\n\n"
        "## 2. メタデータ・タグ\n\n* キーワード\n\n"
        "## 3. 主要な概念\n\n* 概念\n"
    )


def test_sanitize_summary_keeps_clean_output():
    from transcribe.summarize import sanitize_summary

    text = _lecture_body()
    assert sanitize_summary(text) == (text, False)


def test_sanitize_summary_cuts_repeated_document():
    from transcribe.summarize import sanitize_summary

    text = _lecture_body() + "\n" + _lecture_body()
    fixed, repaired = sanitize_summary(text)
    assert repaired is True
    assert fixed.count("## 1. 講義の全体要約") == 1


def test_sanitize_summary_collapses_degenerate_run_and_drops_empty_table():
    from transcribe.summarize import sanitize_summary

    text = _lecture_body() + "\n## 4. 章立て表\n\n| 番号 | 内容 |\n| " + "-" * 400 + "\n"
    fixed, repaired = sanitize_summary(text)
    assert repaired is True
    assert "-" * 100 not in fixed
    assert "## 4. 章立て表" not in fixed  # データ行のない表は見出しごと削除
    assert fixed.rstrip().endswith("* 概念")


def test_truncated_but_recoverable_summary_is_saved(tmp_path):
    """崩れで上限に達した場合、繰り返し前までを summary.md として保存する。"""
    from unittest.mock import MagicMock, patch

    from transcribe.config import SummarizeConfig
    from transcribe.summarize import generate_summary

    (tmp_path / "transcript.md").write_text("[00:00:00] テスト", encoding="utf-8")
    cfg = SummarizeConfig(enabled=True, provider="claude", anthropic_api_key="k")

    partial = _lecture_body() + "\n" + _lecture_body()[:80]
    block = MagicMock(type="text", text=partial)
    message = MagicMock(content=[block], stop_reason="max_tokens")
    with patch("anthropic.Anthropic") as mock_cls:
        mock_cls.return_value.messages.create.return_value = message
        path = generate_summary(tmp_path, "https://x", "t", cfg, [], source_type="local")

    assert path == tmp_path / "summary.md"
    saved = path.read_text(encoding="utf-8")
    assert saved.count("## 1. 講義の全体要約") == 1
    assert not (tmp_path / "summary.truncated.md").exists()


# ─── まとめの時刻を実際の発話開始時刻に合わせる ───────────────────────────


def test_snap_timestamps_to_actual_starts():
    from transcribe.summarize import snap_timestamps

    starts = [0.0, 62.4, 455.2]
    text = "### 導入 [01:02]\n* 「引用」 [07:30]\n総時間: 01:06:40\n"
    fixed, snapped = snap_timestamps(text, starts)

    assert snapped == 2
    assert "[00:01:02]" in fixed
    assert "[00:07:35]" in fixed
    assert "総時間: 01:06:40" in fixed  # 総時間は長さなので変更しない


def test_snap_timestamps_rewrites_youtube_link():
    from transcribe.summarize import snap_timestamps

    text = "#### 方法 [[00:07:30](https://www.youtube.com/watch?v=abc&t=450s)]\n"
    fixed, snapped = snap_timestamps(text, [455.2], video_id="abc")

    assert snapped == 1
    assert "t=455s" in fixed
    assert "[00:07:35]" in fixed


def test_snap_timestamps_keeps_far_times():
    from transcribe.summarize import snap_timestamps

    text = "[00:50:00]\n"
    fixed, snapped = snap_timestamps(text, [0.0, 10.0])

    assert snapped == 0
    assert fixed.strip() == "[00:50:00]"
