from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from .utils import normalize_youtube_url

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    video_id        TEXT,
    title           TEXT,
    status          TEXT NOT NULL,
    source_type     TEXT NOT NULL DEFAULT 'youtube',
    retry_count     INTEGER DEFAULT 0,
    error_message   TEXT,
    output_dir      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
"""

# queued / downloading / separating / transcribing / formatting / done / failed
JobStatus = str


@contextmanager
def _connect(db_path: Path) -> Generator[sqlite3.Connection, None, None]:
    # Web サーバーと CLI サブプロセスが同じ DB を同時に使うため、ロック待ちを長めに取る
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate_source_type(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "source_type" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN source_type TEXT NOT NULL DEFAULT 'youtube'")
        logger.info("マイグレーション: source_type カラムを追加しました")


def _migrate_synced_at(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "synced_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN synced_at TEXT")
        logger.info("マイグレーション: synced_at カラムを追加しました")


def _migrate_summarized_at(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "summarized_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN summarized_at TEXT")
        logger.info("マイグレーション: summarized_at カラムを追加しました")


def _migrate_notion_synced_at(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "notion_synced_at" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN notion_synced_at TEXT")
        logger.info("マイグレーション: notion_synced_at カラムを追加しました")


def _migrate_post_error(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    if "post_error" not in cols:
        conn.execute("ALTER TABLE jobs ADD COLUMN post_error TEXT")
        logger.info("マイグレーション: post_error カラムを追加しました")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        # WAL: 書き込み中も読み込み（Web UI のジョブ一覧など）がブロックされない
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        _migrate_source_type(conn)
        _migrate_synced_at(conn)
        _migrate_summarized_at(conn)
        _migrate_notion_synced_at(conn)
        _migrate_post_error(conn)
    logger.debug(f"SQLite 初期化完了: {db_path}")


def upsert_job(db_path: Path, url: str, *, source_type: str = "youtube") -> int:
    """URLを登録。既存なら何もしない。job id を返す。"""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO jobs (url, status, source_type) VALUES (?, 'queued', ?)",
            (url, source_type),
        )
        row = conn.execute("SELECT id FROM jobs WHERE url = ?", (url,)).fetchone()
    return row["id"]


def get_pending_jobs(db_path: Path) -> list[sqlite3.Row]:
    """queued / 中断中のジョブ一覧を返す"""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE status NOT IN ('done', 'failed') ORDER BY id"
        ).fetchall()
    return rows


def get_all_jobs(db_path: Path) -> list[sqlite3.Row]:
    with _connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
    return rows


def get_job_by_id(db_path: Path, job_id: int) -> sqlite3.Row | None:
    with _connect(db_path) as conn:
        return conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()


def get_job_by_url_or_id(db_path: Path, url_or_id: str) -> sqlite3.Row | None:
    """URL完全一致を優先し、次に video_id で検索する。"""
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM jobs WHERE url = ?", (url_or_id,)).fetchone()
        if row is None:
            normalized = normalize_youtube_url(url_or_id)
            if normalized != url_or_id:
                row = conn.execute("SELECT * FROM jobs WHERE url = ?", (normalized,)).fetchone()
        if row is None:
            row = conn.execute(
                "SELECT * FROM jobs WHERE video_id = ?", (url_or_id,)
            ).fetchone()
    return row


def reset_for_rerun(db_path: Path, job_id: int) -> None:
    """ジョブを完全初期化する（rerun 用。retry_count も 0 にリセット）。"""
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'queued',
                   retry_count = 0,
                   error_message = NULL,
                   output_dir = NULL,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (job_id,),
        )


def update_status(
    db_path: Path,
    job_id: int,
    status: JobStatus,
    *,
    video_id: str | None = None,
    title: str | None = None,
    output_dir: str | None = None,
) -> None:
    fields = ["status = ?", "updated_at = CURRENT_TIMESTAMP"]
    values: list = [status]

    if video_id is not None:
        fields.append("video_id = ?")
        values.append(video_id)
    if title is not None:
        fields.append("title = ?")
        values.append(title)
    if output_dir is not None:
        fields.append("output_dir = ?")
        values.append(output_dir)

    values.append(job_id)
    sql = f"UPDATE jobs SET {', '.join(fields)} WHERE id = ?"
    with _connect(db_path) as conn:
        conn.execute(sql, values)


def record_error(db_path: Path, job_id: int, message: str, *, increment_retry: bool = True) -> None:
    retry_expr = "retry_count + 1" if increment_retry else "retry_count"
    with _connect(db_path) as conn:
        conn.execute(
            f"""UPDATE jobs
               SET status = 'failed',
                   error_message = ?,
                   retry_count = {retry_expr},
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (message, job_id),
        )


def reset_for_retry(db_path: Path, job_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'queued',
                   error_message = NULL,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (job_id,),
        )


def get_unsynced_done_jobs(db_path: Path) -> list[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE status = 'done' AND synced_at IS NULL ORDER BY id"
        ).fetchall()


def get_all_done_jobs(db_path: Path) -> list[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE status = 'done' ORDER BY id"
        ).fetchall()


def record_synced(db_path: Path, job_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE jobs
               SET synced_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (job_id,),
        )


def get_unsummarized_done_jobs(db_path: Path) -> list[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE status = 'done' AND summarized_at IS NULL ORDER BY id"
        ).fetchall()


def record_summarized(db_path: Path, job_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE jobs
               SET summarized_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (job_id,),
        )


def get_unnotion_synced_done_jobs(db_path: Path) -> list[sqlite3.Row]:
    with _connect(db_path) as conn:
        return conn.execute(
            "SELECT * FROM jobs WHERE status = 'done' AND notion_synced_at IS NULL ORDER BY id"
        ).fetchall()


def record_notion_synced(db_path: Path, job_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE jobs
               SET notion_synced_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (job_id,),
        )


def record_post_error(db_path: Path, job_id: int, message: str | None) -> None:
    """まとめ生成・同期など後処理の失敗内容を記録する（None でクリア）。"""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE jobs SET post_error = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (message, job_id),
        )


def delete_job(db_path: Path, job_id: int) -> None:
    with _connect(db_path) as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def get_source_type(job) -> str:
    """sqlite3.Row または dict から source_type を安全に取得する。"""
    try:
        return job["source_type"] or "youtube"
    except (KeyError, IndexError):
        return "youtube"
