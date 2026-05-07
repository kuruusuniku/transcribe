from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    url             TEXT NOT NULL UNIQUE,
    video_id        TEXT,
    title           TEXT,
    status          TEXT NOT NULL,
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
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
    logger.debug(f"SQLite 初期化完了: {db_path}")


def upsert_job(db_path: Path, url: str) -> int:
    """URLを登録。既存なら何もしない。job id を返す。"""
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO jobs (url, status) VALUES (?, 'queued')",
            (url,),
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


def record_error(db_path: Path, job_id: int, message: str) -> None:
    with _connect(db_path) as conn:
        conn.execute(
            """UPDATE jobs
               SET status = 'failed',
                   error_message = ?,
                   retry_count = retry_count + 1,
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
