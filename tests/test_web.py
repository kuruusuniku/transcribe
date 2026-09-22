from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from transcribe.config import (
    AppConfig,
    AudioSeparationConfig,
    GoogleDocsConfig,
    LoggingConfig,
    OutputConfig,
    PathsConfig,
    RetryConfig,
    SummarizeConfig,
    TranscriptionConfig,
    VadParameters,
    WebConfig,
    YoutubeConfig,
)
from transcribe.state import init_db, update_status, upsert_job
from transcribe.web.app import app
from transcribe.web.deps import get_config


# ─── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def tmp_db(tmp_path):
    db = tmp_path / "state.db"
    init_db(db)
    return db


@pytest.fixture
def tmp_output(tmp_path):
    out = tmp_path / "output"
    out.mkdir()
    return out


@pytest.fixture
def mock_config(tmp_path, tmp_db, tmp_output):
    work = tmp_path / "work"
    work.mkdir()
    logs = tmp_path / "logs"
    logs.mkdir()

    cfg = AppConfig(
        paths=PathsConfig(
            work_dir=work,
            output_dir=tmp_output,
            state_db=tmp_db,
            log_dir=logs,
        ),
        youtube=YoutubeConfig(),
        audio_separation=AudioSeparationConfig(),
        transcription=TranscriptionConfig(
            vad_parameters=VadParameters()
        ),
        output=OutputConfig(),
        retry=RetryConfig(),
        logging=LoggingConfig(),
        google_docs=GoogleDocsConfig(),
        summarize=SummarizeConfig(),
        web=WebConfig(),
    )
    return cfg


@pytest.fixture
def client(mock_config):
    app.dependency_overrides[get_config] = lambda: mock_config
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def done_job(mock_config, tmp_output):
    """完了済みジョブをDBに登録し、transcript.md を作成する。"""
    job_id = upsert_job(mock_config.state_db, "https://example.com/watch?v=test1")
    out_dir = tmp_output / f"job_{job_id}"
    out_dir.mkdir()
    (out_dir / "transcript.md").write_text("# テスト文字起こし\n本文", encoding="utf-8")
    update_status(mock_config.state_db, job_id, "done", output_dir=str(out_dir))
    return job_id, out_dir


# ─── 1. GET /api/jobs ─────────────────────────────────────────────────────


def test_list_jobs_empty(client):
    res = client.get("/api/jobs")
    assert res.status_code == 200
    assert res.json() == []


def test_list_jobs(client, done_job):
    res = client.get("/api/jobs")
    assert res.status_code == 200
    data = res.json()
    assert len(data) == 1
    assert data[0]["status"] == "done"


# ─── 2. GET /api/jobs/{id} ────────────────────────────────────────────────


def test_get_job(client, done_job):
    job_id, _ = done_job
    res = client.get(f"/api/jobs/{job_id}")
    assert res.status_code == 200
    assert res.json()["id"] == job_id


# ─── 3. GET /api/jobs/{id} 存在しない ID ─────────────────────────────────


def test_get_job_not_found(client):
    res = client.get("/api/jobs/99999")
    assert res.status_code == 404


# ─── 4. GET /api/jobs/{id}/transcript ────────────────────────────────────


def test_get_transcript(client, done_job):
    job_id, _ = done_job
    res = client.get(f"/api/jobs/{job_id}/transcript")
    assert res.status_code == 200
    assert "テスト文字起こし" in res.json()["content"]


# ─── 5. GET /api/jobs/{id}/transcript — summary.md 不在で 404 ────────────


def test_get_transcript_missing(client, mock_config):
    job_id = upsert_job(mock_config.state_db, "https://example.com/watch?v=noout")
    update_status(mock_config.state_db, job_id, "done", output_dir="/nonexistent/path")
    res = client.get(f"/api/jobs/{job_id}/transcript")
    assert res.status_code == 404


# ─── 6. GET /api/jobs/{id}/summary ───────────────────────────────────────


def test_get_summary(client, done_job):
    job_id, out_dir = done_job
    (out_dir / "summary.md").write_text("# テストまとめ", encoding="utf-8")
    res = client.get(f"/api/jobs/{job_id}/summary")
    assert res.status_code == 200
    assert "テストまとめ" in res.json()["content"]


# ─── 7. POST /api/run — task_id が返ること ───────────────────────────────


def test_run_command(client):
    with patch("transcribe.web.routes.commands.start_task", return_value="task-abc") as mock_st:
        res = client.post("/api/run", json={"urls": ["https://example.com/watch?v=abc"]})
    assert res.status_code == 200
    assert res.json()["task_id"] == "task-abc"
    mock_st.assert_called_once()


# ─── 8. POST /api/run — URL なしで 422 ───────────────────────────────────


def test_run_empty_urls(client):
    res = client.post("/api/run", json={"urls": []})
    assert res.status_code == 422


def test_run_missing_urls(client):
    res = client.post("/api/run", json={})
    assert res.status_code == 422


# ─── 9. POST /api/run/file — mp3 アップロードで task_id ─────────────────


def test_run_file_mp3(client):
    with patch("transcribe.web.routes.files.start_task", return_value="task-file") as mock_st:
        res = client.post(
            "/api/run/file",
            files={"audio": ("test.mp3", b"fake-mp3-data", "audio/mpeg")},
        )
    assert res.status_code == 200
    assert res.json()["task_id"] == "task-file"


# ─── 10. POST /api/run/file — 非対応拡張子で 400 ─────────────────────────


def test_run_file_invalid_ext(client):
    res = client.post(
        "/api/run/file",
        files={"audio": ("test.wav", b"fake-data", "audio/wav")},
    )
    assert res.status_code == 400


# ─── 11. POST /api/sync — task_id が返ること ─────────────────────────────


def test_sync(client):
    with patch("transcribe.web.routes.commands.start_task", return_value="task-sync"):
        res = client.post("/api/sync", json={"all": False})
    assert res.status_code == 200
    assert "task_id" in res.json()


# ─── 12. POST /api/summarize — task_id が返ること ────────────────────────


def test_summarize(client):
    with patch("transcribe.web.routes.commands.start_task", return_value="task-sum"):
        res = client.post("/api/summarize", json={"all": False})
    assert res.status_code == 200
    assert "task_id" in res.json()


# ─── 13. POST /api/rerun — task_id が返ること ────────────────────────────


def test_rerun(client):
    with patch("transcribe.web.routes.commands.start_task", return_value="task-rerun"):
        res = client.post("/api/rerun", json={"url_or_id": "https://example.com/watch?v=abc"})
    assert res.status_code == 200
    assert "task_id" in res.json()


# ─── 14. GET / — index.html が返ること ───────────────────────────────────


def test_index(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert b"<title>transcribe</title>" in res.content
    assert b'src="/static/app.jsx"' in res.content


# ─── 15. WS /ws/logs/{task_id} — ログが流れて [完了] で終了 ──────────────


class _MockQueue:
    """asyncio.Queue の代わりに使えるテスト用キュー。"""

    def __init__(self, messages: list):
        self._msgs = iter(messages)

    async def get(self):
        try:
            return next(self._msgs)
        except StopIteration:
            return None


def test_ws_logs_known_task(client):
    from transcribe.web.runner import active_tasks

    task_id = "test-task-ws"
    mock_q = _MockQueue(["line 1", "line 2", None])
    active_tasks[task_id] = mock_q

    try:
        with client.websocket_connect(f"/ws/logs/{task_id}") as ws:
            assert ws.receive_text() == "line 1"
            assert ws.receive_text() == "line 2"
            assert ws.receive_text() == "[完了]"
    finally:
        active_tasks.pop(task_id, None)


# ─── 16. WS /ws/logs/{不明なtask_id} — エラーで切断 ─────────────────────


def test_ws_logs_unknown_task(client):
    with client.websocket_connect("/ws/logs/nonexistent-task-id") as ws:
        msg = ws.receive_text()
        assert msg == "[タスクが見つかりません]"


# ─── WebSocket /ws/logs/global が task_id ルートに奪われないこと ─────────


def test_ws_global_registers_subscriber(client):
    from transcribe.web.runner import global_subscribers

    import time

    with client.websocket_connect("/ws/logs/global"):
        for _ in range(50):
            if global_subscribers:
                break
            time.sleep(0.01)
        assert len(global_subscribers) == 1


# ─── アクセス制御 ─────────────────────────────────────────────────────────


def test_cross_origin_post_rejected(client):
    res = client.post("/api/clean", headers={"Origin": "https://evil.example"})
    assert res.status_code == 403


def test_same_origin_post_allowed(client):
    with patch("transcribe.web.routes.commands.start_task", return_value="t"):
        res = client.post("/api/clean", headers={"Origin": "http://testserver"})
    assert res.status_code == 200


@pytest.fixture
def token_client(client):
    app.state.auth_token = "secret"
    yield client
    app.state.auth_token = None


def test_token_required(token_client):
    assert token_client.get("/api/jobs").status_code == 401


def test_token_bearer(token_client):
    res = token_client.get("/api/jobs", headers={"Authorization": "Bearer secret"})
    assert res.status_code == 200


def test_token_query_sets_cookie(token_client):
    res = token_client.get("/?token=secret", follow_redirects=False)
    assert res.status_code == 303
    assert "transcribe_token" in res.cookies
    assert token_client.get("/api/jobs").status_code == 200


def test_token_wrong(token_client):
    assert token_client.get("/?token=wrong", follow_redirects=False).status_code == 401


def test_glossary_put_invalid_regex_rejected(client):
    res = client.put("/api/glossary", json={
        "context": "",
        "substitutions": [{"pattern": "(", "replacement": "x", "type": "regex"}],
        "important_terms": [],
    })
    assert res.status_code == 422


# ─── UI 用 API ────────────────────────────────────────────────────────────


def test_jobs_include_stages_and_attention(client, mock_config, tmp_db):
    from transcribe.state import finish_stage

    job_id = upsert_job(tmp_db, "https://www.youtube.com/watch?v=attention01")
    update_status(tmp_db, job_id, "done", output_dir=str(mock_config.output_dir))
    finish_stage(tmp_db, job_id, "notion_sync", "failed", error="401")

    job = next(j for j in client.get("/api/jobs").json() if j["id"] == job_id)
    assert job["stages"]["notion_sync"]["status"] == "failed"
    assert job["attention"] == ["Notion 同期に失敗しました"]
    assert job["in_progress"] is False


def test_queue_status(client):
    res = client.get("/api/queue")
    assert res.status_code == 200
    assert set(res.json()) == {"running", "pending"}


def test_health_lists_checks(client):
    keys = {c["key"] for c in client.get("/api/health").json()}
    assert {"ffmpeg", "summarize", "notion", "docs"} <= keys


def test_resume_post_starts_task(client):
    with patch("transcribe.web.routes.commands.start_task", return_value="t-post") as mock_st:
        res = client.post("/api/resume-post", json={"job_id": 3})
    assert res.json()["task_id"] == "t-post"
    assert mock_st.call_args.args[0] == ["resume-post", "3"]


def test_add_substitution_applies_to_transcript(client, mock_config, tmp_db, tmp_path):
    glossary = tmp_path / "glossary.yaml"
    glossary.write_text("context: ''\nsubstitutions: []\nimportant_terms: []\n", encoding="utf-8")
    out = mock_config.output_dir / "job1"
    out.mkdir()
    (out / "transcript.md").write_text("最下丹田と最下丹田", encoding="utf-8")
    job_id = upsert_job(tmp_db, "https://www.youtube.com/watch?v=glossary001")
    update_status(tmp_db, job_id, "done", output_dir=str(out))

    with patch("transcribe.web.routes.glossary.GLOSSARY_PATH", glossary):
        res = client.post("/api/glossary/substitutions", json={
            "pattern": "最下丹田", "replacement": "臍下丹田", "apply_to_job_id": job_id,
        })
    assert res.json() == {"ok": True, "replaced": 2}
    assert (out / "transcript.md").read_text(encoding="utf-8") == "臍下丹田と臍下丹田"
    assert "臍下丹田" in glossary.read_text(encoding="utf-8")


def test_run_file_rejects_duplicate_audio(client, mock_config, tmp_db, tmp_path):
    existing = tmp_path / "already.mp3"
    existing.write_bytes(b"same-audio")
    job_id = upsert_job(tmp_db, str(existing), source_type="local")
    update_status(tmp_db, job_id, "done")
    from transcribe.pipeline import file_content_hash
    from transcribe.state import record_content_hash

    record_content_hash(tmp_db, job_id, file_content_hash(existing))

    with patch("transcribe.web.routes.files.start_task", return_value="t") as mock_st:
        res = client.post("/api/run/file", files={"audio": ("new-name.mp3", b"same-audio", "audio/mpeg")})

    body = res.json()
    assert body["duplicate_job_id"] == job_id
    assert body["task_id"] is None
    mock_st.assert_not_called()
    # 重複時はアップロードしたファイルを残さない
    assert not any((mock_config.work_dir.parent / "uploads").glob("*/*")) if (mock_config.work_dir.parent / "uploads").exists() else True


def test_run_file_accepts_new_audio(client):
    with patch("transcribe.web.routes.files.start_task", return_value="t-new") as mock_st:
        res = client.post("/api/run/file", files={"audio": ("fresh.mp3", b"unique-audio-bytes", "audio/mpeg")})
    assert res.json()["task_id"] == "t-new"
    cmd = mock_st.call_args.args[0]
    assert cmd[0] == "file" and cmd[1].endswith("fresh.mp3")
