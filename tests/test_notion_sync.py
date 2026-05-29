from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from transcribe.config import NotionConfig
from transcribe.notion_sync import (
    _build_properties,
    _parse_rich_text,
    _query_notion_db,
    md_to_blocks,
    sync_to_notion,
)
from transcribe.state import (
    get_job_by_id,
    init_db,
    record_notion_synced,
    update_status,
    upsert_job,
)


# ─── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def db(tmp_path):
    db_path = tmp_path / "state.db"
    init_db(db_path)
    return db_path


@pytest.fixture
def notion_cfg():
    return NotionConfig(
        enabled=True,
        token="secret_test",
        database_id="3693a9eeef9a80bca8bbf2919d5bef77",
    )


@pytest.fixture
def output_dir_with_summary(tmp_path):
    d = tmp_path / "output" / "2026-05-23_abc123"
    d.mkdir(parents=True)
    (d / "summary.md").write_text(
        "# テスト動画まとめ\n\n## ポイント\n\n- 重要事項1\n- 重要事項2\n",
        encoding="utf-8",
    )
    (d / "meta.json").write_text(
        json.dumps({
            "title": "テスト動画",
            "recording_date": "2026-05-01",
            "source_type": "youtube",
            "url": "https://www.youtube.com/watch?v=abc123",
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    return d


@pytest.fixture
def output_dir_no_summary(tmp_path):
    d = tmp_path / "output" / "2026-05-23_xyz"
    d.mkdir(parents=True)
    (d / "meta.json").write_text("{}", encoding="utf-8")
    return d


# ─── md_to_blocks ─────────────────────────────────────────────────────────


def test_md_to_blocks_headings():
    blocks = md_to_blocks("# H1\n## H2\n### H3")
    types = [b["type"] for b in blocks]
    assert types == ["heading_1", "heading_2", "heading_3"]
    assert blocks[0]["heading_1"]["rich_text"][0]["text"]["content"] == "H1"
    assert blocks[1]["heading_2"]["rich_text"][0]["text"]["content"] == "H2"
    assert blocks[2]["heading_3"]["rich_text"][0]["text"]["content"] == "H3"


def test_md_to_blocks_bullets():
    blocks = md_to_blocks("- item1\n* item2\n1. numbered")
    assert blocks[0]["type"] == "bulleted_list_item"
    assert blocks[1]["type"] == "bulleted_list_item"
    assert blocks[2]["type"] == "numbered_list_item"
    assert blocks[2]["numbered_list_item"]["rich_text"][0]["text"]["content"] == "numbered"


def test_md_to_blocks_paragraph():
    blocks = md_to_blocks("普通のテキスト")
    assert blocks[0]["type"] == "paragraph"
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == "普通のテキスト"


def test_md_to_blocks_empty_lines_skipped():
    blocks = md_to_blocks("line1\n\nline2")
    assert len(blocks) == 2
    assert blocks[0]["paragraph"]["rich_text"][0]["text"]["content"] == "line1"
    assert blocks[1]["paragraph"]["rich_text"][0]["text"]["content"] == "line2"


def test_md_to_blocks_code_block():
    blocks = md_to_blocks("```python\nprint('hello')\n```")
    assert len(blocks) == 1
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "python"
    assert "print" in blocks[0]["code"]["rich_text"][0]["text"]["content"]


def test_md_to_blocks_code_block_no_lang():
    blocks = md_to_blocks("```\ncode here\n```")
    assert blocks[0]["type"] == "code"
    assert blocks[0]["code"]["language"] == "plain text"


def test_md_to_blocks_empty_text():
    assert md_to_blocks("") == []


# ─── _parse_rich_text ─────────────────────────────────────────────────────


def test_parse_rich_text_plain():
    parts = _parse_rich_text("テキスト")
    assert len(parts) == 1
    assert parts[0]["text"]["content"] == "テキスト"
    assert "annotations" not in parts[0]


def test_parse_rich_text_bold():
    parts = _parse_rich_text("前 **太字** 後")
    texts = [p["text"]["content"] for p in parts]
    assert "太字" in texts
    bold_parts = [p for p in parts if p.get("annotations", {}).get("bold")]
    assert len(bold_parts) == 1
    assert bold_parts[0]["text"]["content"] == "太字"


def test_parse_rich_text_2000_char_split():
    long_text = "a" * 4500
    parts = _parse_rich_text(long_text)
    assert len(parts) == 3
    assert all(len(p["text"]["content"]) <= 2000 for p in parts)


def test_parse_rich_text_empty():
    parts = _parse_rich_text("")
    assert len(parts) == 1
    assert parts[0]["text"]["content"] == ""


# ─── _build_properties ────────────────────────────────────────────────────


def test_build_properties_youtube():
    props = _build_properties(
        title="テスト",
        date_str="2026-05-01",
        video_url="https://www.youtube.com/watch?v=abc",
        duration_min=30,
        source_type="youtube",
    )
    assert props["名前"]["title"][0]["text"]["content"] == "テスト"
    assert props["日付"]["date"]["start"] == "2026-05-01"
    assert props["URL"]["url"] == "https://www.youtube.com/watch?v=abc"
    assert props["動画時間"]["number"] == 30
    assert props["ソース種別"]["select"]["name"] == "youtube"
    assert props["まとめ進捗"]["checkbox"] is True
    assert props["タグ"]["multi_select"] == []


def test_build_properties_local_no_url():
    props = _build_properties(
        title="ローカル",
        date_str=None,
        video_url="C:/audio/test.mp3",
        duration_min=None,
        source_type="local",
    )
    assert "URL" not in props
    assert "日付" not in props
    assert "動画時間" not in props


def test_build_properties_unknown_date_omitted():
    props = _build_properties(
        title="test",
        date_str="不明",
        video_url="https://x.com/v",
        duration_min=None,
        source_type="youtube",
    )
    assert "日付" not in props


def test_build_properties_title_truncated():
    long_title = "t" * 2500
    props = _build_properties(
        title=long_title,
        date_str=None,
        video_url="https://x.com/v",
        duration_min=None,
        source_type="youtube",
    )
    assert len(props["名前"]["title"][0]["text"]["content"]) == 2000


# ─── sync_to_notion: summary.md なしはスキップ ────────────────────────────


def test_sync_to_notion_no_summary_returns_false(output_dir_no_summary, notion_cfg):
    result = sync_to_notion(output_dir_no_summary, "https://x.com/v", notion_cfg)
    assert result is False


# ─── sync_to_notion: 新規ページ作成 ──────────────────────────────────────


@patch("transcribe.notion_sync._query_notion_db")
@patch("transcribe.notion_sync.Client")
def test_sync_to_notion_creates_new_page(mock_client_cls, mock_query_db, output_dir_with_summary, notion_cfg):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    mock_query_db.return_value = []
    mock_client.pages.create.return_value = {"id": "new-page-id"}

    result = sync_to_notion(
        output_dir_with_summary,
        "https://www.youtube.com/watch?v=abc123",
        notion_cfg,
    )

    assert result is True
    mock_client.pages.create.assert_called_once()
    create_kwargs = mock_client.pages.create.call_args.kwargs
    assert create_kwargs["parent"] == {"database_id": notion_cfg.database_id}
    assert create_kwargs["properties"]["名前"]["title"][0]["text"]["content"] == "テスト動画"
    assert create_kwargs["properties"]["まとめ進捗"]["checkbox"] is True


# ─── sync_to_notion: 既存ページ更新 ──────────────────────────────────────


@patch("transcribe.notion_sync._query_notion_db")
@patch("transcribe.notion_sync.Client")
def test_sync_to_notion_updates_existing_page(mock_client_cls, mock_query_db, output_dir_with_summary, notion_cfg):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client

    mock_query_db.return_value = [{"id": "existing-page-id"}]
    mock_client.blocks.children.list.return_value = {"results": [], "has_more": False}

    result = sync_to_notion(
        output_dir_with_summary,
        "https://www.youtube.com/watch?v=abc123",
        notion_cfg,
    )

    assert result is True
    mock_client.pages.update.assert_called_once()
    update_kwargs = mock_client.pages.update.call_args.kwargs
    assert update_kwargs["page_id"] == "existing-page-id"
    mock_client.pages.create.assert_not_called()


# ─── sync_to_notion: ローカルファイルは URL 検索しない ────────────────────


@patch("transcribe.notion_sync._query_notion_db")
@patch("transcribe.notion_sync.Client")
def test_sync_to_notion_local_skips_url_search(mock_client_cls, mock_query_db, tmp_path, notion_cfg):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.pages.create.return_value = {"id": "page-id"}

    d = tmp_path / "local_out"
    d.mkdir()
    (d / "summary.md").write_text("# まとめ", encoding="utf-8")

    result = sync_to_notion(d, "C:/audio/test.mp3", notion_cfg)

    assert result is True
    mock_query_db.assert_not_called()


# ─── state.py: notion_synced_at ───────────────────────────────────────────


def test_notion_synced_at_migration(db):
    job_id = upsert_job(db, "https://example.com/notion/1")
    update_status(db, job_id, "done", output_dir="/tmp/out")
    job = get_job_by_id(db, job_id)
    assert job["notion_synced_at"] is None


def test_record_notion_synced(db):
    job_id = upsert_job(db, "https://example.com/notion/2")
    update_status(db, job_id, "done", output_dir="/tmp/out")
    record_notion_synced(db, job_id)
    job = get_job_by_id(db, job_id)
    assert job["notion_synced_at"] is not None


def test_get_unnotion_synced_done_jobs(db):
    from transcribe.state import get_unnotion_synced_done_jobs

    j1 = upsert_job(db, "https://example.com/notion/a")
    j2 = upsert_job(db, "https://example.com/notion/b")
    j3 = upsert_job(db, "https://example.com/notion/c")
    update_status(db, j1, "done", output_dir="/tmp/a")
    update_status(db, j2, "done", output_dir="/tmp/b")
    update_status(db, j3, "queued")

    record_notion_synced(db, j1)

    unsynced = get_unnotion_synced_done_jobs(db)
    assert len(unsynced) == 1
    assert unsynced[0]["id"] == j2


# ─── pipeline: best-effort でパイプラインが継続 ────────────────────────────


def test_pipeline_notion_sync_failure_does_not_fail_job(db, tmp_path):
    job_id = upsert_job(db, "https://example.com/notion/fail")
    update_status(db, job_id, "done", output_dir=str(tmp_path))

    from transcribe.notion_sync import sync_to_notion as real_sync
    from transcribe.config import NotionConfig

    cfg_n = NotionConfig(enabled=True, token="tok", database_id="dbid")

    with patch("transcribe.notion_sync.Client") as mock_cls:
        mock_cls.side_effect = Exception("network error")
        try:
            real_sync(tmp_path / "no_summary_here", "https://x.com/v", cfg_n)
        except Exception:
            pass

    job = get_job_by_id(db, job_id)
    assert job["status"] == "done"
    assert job["notion_synced_at"] is None


# ─── CLI sync-notion command ──────────────────────────────────────────────


def _make_cfg_with_notion(tmp_path, *, enabled=True):
    from transcribe.config import (
        AppConfig,
        AudioSeparationConfig,
        GoogleDocsConfig,
        LoggingConfig,
        NotionConfig,
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
        notion=NotionConfig(
            enabled=enabled,
            token="secret_test" if enabled else "",
            database_id="dbid123" if enabled else "",
        ),
    )
    return cfg


def test_cli_sync_notion_disabled(tmp_path):
    from typer.testing import CliRunner
    from transcribe.cli import app

    cfg = _make_cfg_with_notion(tmp_path, enabled=False)
    init_db(cfg.state_db)
    runner = CliRunner()

    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, MagicMock())):
        result = runner.invoke(app, ["sync-notion"])
    assert "無効" in result.output


def test_cli_sync_notion_no_token(tmp_path):
    from typer.testing import CliRunner
    from transcribe.cli import app
    from transcribe.config import NotionConfig

    cfg = _make_cfg_with_notion(tmp_path, enabled=True)
    cfg.notion.token = ""
    init_db(cfg.state_db)
    runner = CliRunner()

    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, MagicMock())):
        result = runner.invoke(app, ["sync-notion"])
    assert "token" in result.output or "database_id" in result.output


@patch("transcribe.cli.sync_to_notion")
def test_cli_sync_notion_unsummarized_only(mock_sync, tmp_path):
    from typer.testing import CliRunner
    from transcribe.cli import app

    cfg = _make_cfg_with_notion(tmp_path)
    init_db(cfg.state_db)

    out_a = tmp_path / "out_a"
    out_a.mkdir()
    out_b = tmp_path / "out_b"
    out_b.mkdir()

    j1 = upsert_job(cfg.state_db, "https://ex.com/n1")
    j2 = upsert_job(cfg.state_db, "https://ex.com/n2")
    update_status(cfg.state_db, j1, "done", title="A", output_dir=str(out_a))
    update_status(cfg.state_db, j2, "done", title="B", output_dir=str(out_b))
    record_notion_synced(cfg.state_db, j1)

    mock_sync.return_value = True

    runner = CliRunner()
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, MagicMock())):
        result = runner.invoke(app, ["sync-notion"])

    assert result.exit_code == 0
    assert mock_sync.call_count == 1


@patch("transcribe.cli.sync_to_notion")
def test_cli_sync_notion_all_flag(mock_sync, tmp_path):
    from typer.testing import CliRunner
    from transcribe.cli import app

    cfg = _make_cfg_with_notion(tmp_path)
    init_db(cfg.state_db)

    for i in range(3):
        out = tmp_path / f"out_{i}"
        out.mkdir()
        jid = upsert_job(cfg.state_db, f"https://ex.com/na{i}")
        update_status(cfg.state_db, jid, "done", title=f"T{i}", output_dir=str(out))

    record_notion_synced(cfg.state_db, 1)

    mock_sync.return_value = True

    runner = CliRunner()
    with patch("transcribe.cli._load_cfg_and_glossary", return_value=(cfg, MagicMock())):
        result = runner.invoke(app, ["sync-notion", "--all"])

    assert result.exit_code == 0
    assert mock_sync.call_count == 3


# ─── _query_notion_db: 503 リトライ ──────────────────────────────────────


def _make_503_response():
    resp = MagicMock()
    resp.status_code = 503
    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
        "503 Service Unavailable", request=MagicMock(), response=resp
    )
    return resp


def _make_ok_response(results):
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"results": results}
    return resp


@patch("transcribe.notion_sync.time.sleep")
@patch("transcribe.notion_sync.httpx.post")
def test_query_notion_db_503_retries_then_succeeds(mock_post, mock_sleep, notion_cfg):
    mock_post.side_effect = [_make_503_response(), _make_ok_response([{"id": "page-id"}])]

    pages = _query_notion_db(notion_cfg, "https://example.com/video", notion_cfg.database_id)

    assert pages == [{"id": "page-id"}]
    assert mock_post.call_count == 2
    mock_sleep.assert_called_once_with(5)


@patch("transcribe.notion_sync.time.sleep")
@patch("transcribe.notion_sync.httpx.post")
def test_query_notion_db_503_max_retries_raises(mock_post, mock_sleep, notion_cfg):
    mock_post.return_value = _make_503_response()

    with pytest.raises(httpx.HTTPStatusError):
        _query_notion_db(notion_cfg, "https://example.com/video", notion_cfg.database_id)

    assert mock_post.call_count == 4  # 初回 + 3回リトライ
    assert mock_sleep.call_count == 3


@patch("transcribe.notion_sync.time.sleep")
@patch("transcribe.notion_sync.httpx.post")
def test_query_notion_db_503_logs_warning(mock_post, mock_sleep, notion_cfg, caplog):
    mock_post.side_effect = [_make_503_response(), _make_ok_response([])]

    with caplog.at_level(logging.WARNING, logger="transcribe.notion_sync"):
        _query_notion_db(notion_cfg, "https://example.com/video", notion_cfg.database_id)

    warning_msgs = [r.message for r in caplog.records if r.levelno == logging.WARNING]
    assert any("503" in m and "retry" in m for m in warning_msgs)
