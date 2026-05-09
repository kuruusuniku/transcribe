from __future__ import annotations

import logging
import time
import traceback
from datetime import date
from pathlib import Path

import yt_dlp.utils

from .config import AppConfig, GlossaryConfig
from .postprocess import compress_repetitions, postprocess
from .stages.downloader import download_audio
from .stages.formatter import format_outputs
from .stages.separator import separate_audio
from .stages.transcriber import transcribe
from .state import (
    get_pending_jobs,
    record_error,
    reset_for_retry,
    update_status,
    upsert_job,
)
from .utils import ensure_dir

logger = logging.getLogger(__name__)

NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ModuleNotFoundError,
    ImportError,
    yt_dlp.utils.UnsupportedError,
    FileNotFoundError,
    KeyError,
    AttributeError,
    TypeError,
)

NON_RETRYABLE_MESSAGE_PATTERNS: tuple[str, ...] = (
    "Couldn't find appropriate backend",
    "CUDA out of memory",
    "No such file or directory",
    "Permission denied",
)


def is_non_retryable(exc: BaseException) -> bool:
    if isinstance(exc, NON_RETRYABLE_EXCEPTIONS):
        return True
    msg = str(exc)
    return any(p in msg for p in NON_RETRYABLE_MESSAGE_PATTERNS)


def _output_dir_for(output_root: Path, upload_date: str | None, video_id: str) -> Path:
    if upload_date and len(upload_date) == 8:
        date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    else:
        date_str = str(date.today())
    return output_root / f"{date_str}_{video_id}"


def run_pipeline(
    urls: list[str],
    cfg: AppConfig,
    glossary: GlossaryConfig,
    *,
    progress=None,
) -> None:
    db = cfg.state_db
    work_dir = ensure_dir(cfg.work_dir)
    output_dir = ensure_dir(cfg.output_dir)

    # URL を DB に登録
    for url in urls:
        upsert_job(db, url)

    pending = get_pending_jobs(db)
    if not pending:
        logger.info("処理対象なし（全ジョブ完了済みまたはURLなし）")
        return

    logger.info(f"{len(pending)} 件のジョブを処理します")

    # Whisper モデルをジョブ間で保持するキャッシュ
    model_cache: dict = {}

    task_id = None
    if progress is not None:
        task_id = progress.add_task("処理中", total=len(pending))

    for job in pending:
        job_id: int = job["id"]
        url: str = job["url"]
        retry_count: int = job["retry_count"]
        max_attempts = cfg.retry.max_attempts

        logger.info(f"[job {job_id}] 開始: {url}")

        success = False
        non_retryable_hit = False
        for attempt in range(max_attempts):
            if attempt > 0:
                wait = cfg.retry.backoff_seconds[min(attempt - 1, len(cfg.retry.backoff_seconds) - 1)]
                logger.warning(f"[job {job_id}] リトライ {attempt}/{max_attempts - 1}  {wait}秒待機")
                time.sleep(wait)
                reset_for_retry(db, job_id)

            try:
                _run_job(job_id, url, db, work_dir, output_dir, cfg, glossary, model_cache)
                success = True
                break
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                logger.error(f"[job {job_id}] 失敗 (attempt {attempt + 1}/{max_attempts}):\n{err_msg}")
                if is_non_retryable(e):
                    logger.error(f"[job {job_id}] リトライ不可能なエラーを検出: {type(e).__name__}")
                    logger.error(f"[job {job_id}] エラー内容: {e}")
                    logger.error(f"[job {job_id}] このエラーはリトライしても解決しないため、即時 failed として記録します")
                    record_error(db, job_id, err_msg)
                    non_retryable_hit = True
                    break
                elif attempt + 1 >= max_attempts:
                    record_error(db, job_id, err_msg)

        if not success and not non_retryable_hit:
            logger.error(f"[job {job_id}] 最大リトライ回数到達。スキップします。")

        if progress is not None and task_id is not None:
            progress.advance(task_id)

    # セッション終了時にWhisperモデルを解放
    if "model" in model_cache:
        del model_cache["model"]


def _run_job(
    job_id: int,
    url: str,
    db: Path,
    work_dir: Path,
    output_root: Path,
    cfg: AppConfig,
    glossary: GlossaryConfig,
    model_cache: dict,
) -> None:
    # 1. ダウンロード
    update_status(db, job_id, "downloading")
    dl_result = download_audio(url, work_dir, cfg)
    update_status(db, job_id, "downloading", video_id=dl_result.video_id, title=dl_result.title)

    # 2. 音声分離（オプション）
    update_status(db, job_id, "separating")
    audio_path = separate_audio(dl_result.audio_path, work_dir, cfg)
    audio_separation_used = cfg.audio_separation.enabled

    # 3→4. 文字起こし + 後処理（ジェネレータチェーン: Whisper出力を逐次後処理）
    update_status(db, job_id, "transcribing")
    raw_gen = transcribe(audio_path, cfg, glossary, model_cache)
    processed_gen = postprocess(raw_gen, cfg, glossary)
    compressed_gen = compress_repetitions(processed_gen)

    # 5. フォーマット出力（ここでジェネレータを消費し、リスト化・ファイル書き出し）
    update_status(db, job_id, "formatting")
    job_output_dir = _output_dir_for(output_root, dl_result.upload_date, dl_result.video_id)
    segment_count = format_outputs(
        segments=compressed_gen,
        video_id=dl_result.video_id,
        url=url,
        title=dl_result.title,
        upload_date=dl_result.upload_date,
        audio_separation_enabled=audio_separation_used,
        cfg=cfg,
        output_dir=job_output_dir,
    )
    logger.info(f"[job {job_id}] セグメント数: {segment_count}")

    update_status(db, job_id, "done", output_dir=str(job_output_dir))
    logger.info(f"[job {job_id}] 完了: {job_output_dir}")

    # 一時ファイル掃除（分離済み音声のみ削除、元音声は保持）
    if audio_separation_used and audio_path != dl_result.audio_path:
        audio_path.unlink(missing_ok=True)
