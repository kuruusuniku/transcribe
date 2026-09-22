from __future__ import annotations

import hashlib
import logging
import shutil
import time
import traceback
from datetime import date
from pathlib import Path

import yt_dlp.utils
from filelock import FileLock, Timeout

from .config import AppConfig, GlossaryConfig
from .postprocess import compress_repetitions, postprocess
from .stages.downloader import download_audio
from .stages.formatter import format_outputs
from .stages.separator import separate_audio
from .stages.transcriber import transcribe
from .state import (
    get_pending_jobs,
    get_source_type,
    record_error,
    record_notion_synced,
    record_summarized,
    record_synced,
    reset_for_retry,
    update_status,
    upsert_job,
)
from .summarize import generate_summary
from .sync import sync_job
from .utils import ensure_dir, normalize_youtube_url

logger = logging.getLogger(__name__)

NON_RETRYABLE_EXCEPTIONS: tuple[type[BaseException], ...] = (
    ModuleNotFoundError,
    ImportError,
    yt_dlp.utils.UnsupportedError,
    FileNotFoundError,
    PermissionError,
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


def is_local_source(url_or_path: str) -> bool:
    return not url_or_path.startswith(("http://", "https://"))


def _output_dir_for(output_root: Path, upload_date: str | None, video_id: str) -> Path:
    if upload_date and len(upload_date) == 8:
        date_str = f"{upload_date[:4]}-{upload_date[4:6]}-{upload_date[6:]}"
    else:
        date_str = str(date.today())
    return output_root / f"{date_str}_{video_id}"


def _local_video_id(src_path: Path) -> str:
    """ローカルファイルの識別子。同名の別ファイルで出力先が衝突しないよう内容ハッシュを付与する。"""
    h = hashlib.sha1()
    with src_path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return f"{src_path.stem}_{h.hexdigest()[:8]}"


def _copy_local_file(source_path: str, work_dir: Path) -> Path:
    # アップロード済みファイルが既に work_dir 内にある場合はコピー不要
    if Path(source_path).resolve().parent == work_dir.resolve():
        return Path(source_path)
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"ソースファイルが見つかりません: {source_path}")
    dest = work_dir / src.name
    shutil.copy2(src, dest)
    logger.info(f"ローカルファイルをコピー: {src.name} → {dest}")
    return dest


def run_pipeline(
    urls: list[str],
    cfg: AppConfig,
    glossary: GlossaryConfig,
    *,
    progress=None,
    resume_all: bool = False,
) -> None:
    """指定URLのジョブを処理する。

    resume_all=True の場合は、指定URL以外の未完了ジョブ（中断分）もまとめて処理する。
    複数プロセスから同時に呼ばれても同じジョブを二重処理しないよう、
    パイプライン全体をファイルロックで直列化する。
    """
    db = cfg.state_db
    work_dir = ensure_dir(cfg.work_dir)
    output_dir = ensure_dir(cfg.output_dir)

    urls = [u if is_local_source(u) else normalize_youtube_url(u) for u in urls]
    for url in urls:
        source_type = "local" if is_local_source(url) else "youtube"
        upsert_job(db, url, source_type=source_type)

    lock = FileLock(str(db.with_suffix(".lock")))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        logger.info("別のプロセスが処理中のため、完了を待機します")
        lock.acquire()
    try:
        _process_pending(urls, cfg, glossary, db, work_dir, output_dir, progress, resume_all)
    finally:
        lock.release()


def _process_pending(
    urls: list[str],
    cfg: AppConfig,
    glossary: GlossaryConfig,
    db: Path,
    work_dir: Path,
    output_dir: Path,
    progress,
    resume_all: bool,
) -> None:
    # ロック取得後に再取得することで、待機中に他プロセスが完了させたジョブを除外する
    pending = get_pending_jobs(db)
    if not resume_all:
        targets = set(urls)
        pending = [job for job in pending if job["url"] in targets]
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

        source_type: str = get_source_type(job)
        logger.info(f"[job {job_id}] 開始: {url} (source_type={source_type})")

        success = False
        non_retryable_hit = False
        for attempt in range(max_attempts):
            if attempt > 0:
                wait = cfg.retry.backoff_seconds[min(attempt - 1, len(cfg.retry.backoff_seconds) - 1)]
                logger.warning(f"[job {job_id}] リトライ {attempt}/{max_attempts - 1}  {wait}秒待機")
                time.sleep(wait)
                reset_for_retry(db, job_id)

            try:
                _run_job(job_id, url, source_type, db, work_dir, output_dir, cfg, glossary, model_cache)
                success = True
                break
            except Exception as e:
                err_msg = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                logger.error(f"[job {job_id}] 失敗 (attempt {attempt + 1}/{max_attempts}):\n{err_msg}")
                if is_non_retryable(e):
                    logger.error(f"[job {job_id}] リトライ不可能なエラーを検出: {type(e).__name__}")
                    logger.error(f"[job {job_id}] エラー内容: {e}")
                    logger.error(f"[job {job_id}] このエラーはリトライしても解決しないため、即時 failed として記録します")
                    record_error(db, job_id, err_msg, increment_retry=False)
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
    source_type: str,
    db: Path,
    work_dir: Path,
    output_root: Path,
    cfg: AppConfig,
    glossary: GlossaryConfig,
    model_cache: dict,
) -> None:
    if source_type == "local":
        # ローカルファイル: ダウンロードスキップ、work_dir にコピー
        update_status(db, job_id, "downloading")
        src_path = Path(url)
        title = src_path.name
        audio_path = _copy_local_file(url, work_dir)
        video_id = _local_video_id(audio_path)
        upload_date = None
        update_status(db, job_id, "downloading", video_id=video_id, title=title)
    else:
        # YouTube: 既存の yt-dlp ダウンロード処理
        update_status(db, job_id, "downloading")
        dl_result = download_audio(url, work_dir, cfg)
        video_id = dl_result.video_id
        title = dl_result.title
        audio_path = dl_result.audio_path
        upload_date = dl_result.upload_date
        update_status(db, job_id, "downloading", video_id=video_id, title=title)

    # 2. 音声分離（オプション）
    update_status(db, job_id, "separating")
    original_audio_path = audio_path
    audio_path = separate_audio(audio_path, work_dir, cfg)
    audio_separation_used = cfg.audio_separation.enabled

    # 3→4. 文字起こし + 後処理（ジェネレータチェーン: Whisper出力を逐次後処理）
    update_status(db, job_id, "transcribing")
    raw_gen = transcribe(audio_path, cfg, glossary, model_cache)
    processed_gen = postprocess(raw_gen, cfg, glossary)
    compressed_gen = compress_repetitions(processed_gen)

    # 5. フォーマット出力（ここでジェネレータを消費し、リスト化・ファイル書き出し）
    update_status(db, job_id, "formatting")
    job_output_dir = _output_dir_for(output_root, upload_date, video_id)
    segment_count = format_outputs(
        segments=compressed_gen,
        video_id=video_id,
        url=url,
        title=title,
        upload_date=upload_date,
        source_type=source_type,
        audio_separation_enabled=audio_separation_used,
        cfg=cfg,
        output_dir=job_output_dir,
    )
    logger.info(f"[job {job_id}] セグメント数: {segment_count}")

    update_status(db, job_id, "done", output_dir=str(job_output_dir))
    logger.info(f"[job {job_id}] 完了: {job_output_dir}")

    # 自動まとめ生成（best-effort: 失敗してもジョブは done のまま）
    if cfg.summarize.enabled and cfg.summarize.resolve_api_key():
        try:
            summary_path = generate_summary(
                output_dir=job_output_dir,
                video_url=url,
                video_title=title,
                cfg=cfg.summarize,
                glossary_entries=glossary.substitutions,
                source_type=source_type,
            )
            if summary_path:
                record_summarized(db, job_id)
                logger.info(f"[job {job_id}] まとめ生成完了: {summary_path}")
        except Exception as e:
            logger.warning(f"[job {job_id}] まとめ生成失敗（文字起こしは完了済み）: {e}")

    # Google Docs 自動同期
    if cfg.google_docs.enabled and cfg.google_docs.root_folder_id:
        try:
            job_dict = {"source_type": source_type}
            doc_id = sync_job(job_dict, job_output_dir, cfg.google_docs)
            if doc_id:
                record_synced(db, job_id)
                logger.info(f"[job {job_id}] Google Docs に同期完了")
        except Exception as e:
            logger.warning(f"[job {job_id}] Google Docs 同期失敗（文字起こしは完了済み）: {e}")

    # Notion 自動同期
    if cfg.notion.enabled and cfg.notion.database_id and cfg.notion.token:
        try:
            from .notion_sync import sync_to_notion
            synced = sync_to_notion(job_output_dir, url, cfg.notion, source_type)
            if synced:
                record_notion_synced(db, job_id)
                logger.info(f"[job {job_id}] Notion に同期完了")
        except Exception as e:
            logger.warning(f"[job {job_id}] Notion 同期失敗（文字起こしは完了済み）: {e}")

    # 一時ファイル掃除（分離済み音声のみ削除、元音声は保持）
    if audio_separation_used and audio_path != original_audio_path:
        audio_path.unlink(missing_ok=True)
