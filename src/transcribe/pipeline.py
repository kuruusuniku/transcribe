from __future__ import annotations

import hashlib
import json
import logging
import time
import traceback
from dataclasses import asdict
from datetime import date
from pathlib import Path

import yt_dlp.utils
from filelock import FileLock, Timeout

from .config import AppConfig, GlossaryConfig
from .postprocess import compress_repetitions, postprocess
from .stages.downloader import download_audio
from .stages.formatter import format_outputs
from .stages.separator import separate_audio
from .stages.transcriber import Segment, transcribe
from .state import (
    get_pending_jobs,
    get_source_type,
    record_error,
    record_notion_synced,
    record_summarized,
    record_synced,
    finish_stage,
    reset_for_retry,
    get_job_by_id,
    get_job_stages,
    run_stage,
    track_stage,
    update_stage_progress,
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


def _resolve_local_file(source_path: str) -> Path:
    """録音ファイルの存在を確認して返す。読むだけなので work_dir へのコピーはしない。"""
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"ソースファイルが見つかりません: {source_path}")
    return src


def pipeline_lock(db: Path) -> FileLock:
    """パイプライン（と作業フォルダの掃除）を直列化するプロセス間ロック。"""
    return FileLock(str(db) + ".lock")


# Whisper モデルのプロセス内キャッシュ。
# CLI（1 コマンド = 1 プロセス）では run_pipeline 終了時に解放し、
# Web サーバー（常駐プロセス）では keep_model_loaded(True) でタスク間に再利用する。
_MODEL_CACHE: dict = {}
_KEEP_MODEL_LOADED = False


def keep_model_loaded(enabled: bool) -> None:
    global _KEEP_MODEL_LOADED
    _KEEP_MODEL_LOADED = enabled


def release_model_cache() -> bool:
    """キャッシュ済みの Whisper モデルを解放する。解放した場合 True。"""
    if "model" not in _MODEL_CACHE:
        return False
    del _MODEL_CACHE["model"]
    import gc  # noqa: PLC0415

    gc.collect()
    try:
        import torch  # noqa: PLC0415

        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except ImportError:
        pass
    logger.info("Whisper モデルを解放しました")
    return True


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

    lock = pipeline_lock(db)
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

    model_cache = _MODEL_CACHE

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

    if not _KEEP_MODEL_LOADED:
        release_model_cache()


def _transcription_signature(cfg: AppConfig, glossary: GlossaryConfig) -> dict:
    """文字起こし結果に影響する設定。キャッシュを再利用してよいかの判定に使う。"""
    t = cfg.transcription
    return {
        "model": t.model,
        "language": t.language,
        "beam_size": t.beam_size,
        "condition_on_previous_text": t.condition_on_previous_text,
        "vad_filter": t.vad_filter,
        "vad_parameters": asdict(t.vad_parameters),
        "initial_prompt": glossary.context.strip(),
        "audio_separation": cfg.audio_separation.enabled,
    }


def _transcription_cache_path(work_dir: Path, video_id: str) -> Path:
    return work_dir / f"{video_id}.whisper.json"


def _load_transcription_cache(path: Path, signature: dict) -> list[Segment] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("signature") != signature:
            return None
        return [Segment(**seg) for seg in data["segments"]]
    except Exception as e:
        logger.warning(f"文字起こしキャッシュを読み込めません（再実行します）: {e}")
        return None


def _save_transcription_cache(path: Path, signature: dict, segments: list[Segment]) -> None:
    payload = {"signature": signature, "segments": [asdict(seg) for seg in segments]}
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


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
    # 1. 音声取得
    update_status(db, job_id, "downloading")
    with track_stage(db, job_id, "download"):
        if source_type == "local":
            # 録音ファイル: ダウンロード不要。元のファイルをそのまま読む
            title = Path(url).name
            audio_path = _resolve_local_file(url)
            video_id = _local_video_id(audio_path)
            upload_date = None
        else:
            dl_result = download_audio(url, work_dir, cfg)
            video_id = dl_result.video_id
            title = dl_result.title
            audio_path = dl_result.audio_path
            upload_date = dl_result.upload_date
    update_status(db, job_id, "downloading", video_id=video_id, title=title)

    # 2〜3. 音声分離 + 文字起こし。
    # Whisper の生出力は work_dir にキャッシュし、後段（出力など）の失敗でリトライする際は再利用する。
    signature = _transcription_signature(cfg, glossary)
    cache_path = _transcription_cache_path(work_dir, video_id)
    raw_segments = _load_transcription_cache(cache_path, signature)
    audio_separation_used = cfg.audio_separation.enabled

    if raw_segments is not None:
        logger.info(f"[job {job_id}] 文字起こしキャッシュを再利用: {cache_path.name}")
        finish_stage(db, job_id, "separate", "skipped")
        finish_stage(db, job_id, "transcribe", "skipped")
    else:
        update_status(db, job_id, "separating")
        with track_stage(db, job_id, "separate"):
            separated_path = separate_audio(audio_path, work_dir, cfg)
        try:
            update_status(db, job_id, "transcribing")
            with track_stage(db, job_id, "transcribe"):
                raw_segments = list(transcribe(
                    separated_path, cfg, glossary, model_cache,
                    on_progress=_progress_recorder(db, job_id, "transcribe"),
                ))
                _save_transcription_cache(cache_path, signature, raw_segments)
        finally:
            # 分離済み音声は文字起こし後は不要。失敗時も残さない（元音声はリトライ用に保持）
            if audio_separation_used and separated_path != audio_path:
                separated_path.unlink(missing_ok=True)

    # 4. 後処理 + フォーマット出力
    update_status(db, job_id, "formatting")
    job_output_dir = _output_dir_for(output_root, upload_date, video_id)
    with track_stage(db, job_id, "format"):
        processed_gen = compress_repetitions(postprocess(raw_segments, cfg, glossary))
        segment_count = format_outputs(
            segments=processed_gen,
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
    cache_path.unlink(missing_ok=True)
    logger.info(f"[job {job_id}] 完了: {job_output_dir}")

    # 5. 後処理（best-effort: 失敗してもジョブは done のまま。結果は job_stages に記録）
    run_post_stages(
        job_id, url, title, source_type, job_output_dir, db, cfg, glossary,
    )


def enabled_post_stages(cfg: AppConfig) -> list[str]:
    """設定で有効になっている後処理ステージ。"""
    stages: list[str] = []
    if cfg.summarize.enabled and cfg.summarize.resolve_api_key():
        stages.append("summarize")
    if cfg.google_docs.enabled and cfg.google_docs.root_folder_id:
        stages.append("docs_sync")
    if cfg.notion.enabled and cfg.notion.database_id and cfg.notion.token:
        stages.append("notion_sync")
    return stages


def run_post_stages(
    job_id: int,
    url: str,
    title: str,
    source_type: str,
    job_output_dir: Path,
    db: Path,
    cfg: AppConfig,
    glossary: GlossaryConfig,
    *,
    only: set[str] | None = None,
) -> dict[str, bool]:
    """有効な後処理（まとめ → Docs → Notion）を順に実行し、ステージごとの成否を返す。

    only を指定した場合はそのステージだけを実行する。
    """
    results: dict[str, bool] = {}
    for stage in enabled_post_stages(cfg):
        if only is not None and stage not in only:
            continue
        if stage == "summarize":
            def fn() -> bool:
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
                return bool(summary_path)
        elif stage == "docs_sync":
            def fn() -> bool:
                doc_id = sync_job({"source_type": source_type}, job_output_dir, cfg.google_docs)
                if doc_id:
                    record_synced(db, job_id)
                return bool(doc_id)
        else:
            def fn() -> bool:
                from .notion_sync import sync_to_notion  # noqa: PLC0415

                synced = sync_to_notion(job_output_dir, url, cfg.notion, source_type)
                if synced:
                    record_notion_synced(db, job_id)
                return bool(synced)

        results[stage] = run_post_stage(db, job_id, stage, fn)
    return results


def pending_post_stages(db: Path, job_id: int, cfg: AppConfig) -> set[str]:
    """有効な後処理のうち、まだ成功していない（失敗・未実行）ステージ。"""
    done = {r["stage"] for r in get_job_stages(db, job_id) if r["status"] == "done"}
    # ステージ記録の導入前に処理されたジョブは、従来の完了時刻カラムで判定する
    job = get_job_by_id(db, job_id)
    if job is not None:
        legacy = {"summarize": "summarized_at", "docs_sync": "synced_at", "notion_sync": "notion_synced_at"}
        stage_names = {r["stage"] for r in get_job_stages(db, job_id)}
        done |= {s for s, col in legacy.items() if s not in stage_names and job[col]}
    return {s for s in enabled_post_stages(cfg) if s not in done}


def _progress_recorder(db: Path, job_id: int, stage: str):
    """進捗を 2% 刻みで DB に記録するコールバックを返す（書き込み頻度を抑える）。"""
    last = -1.0

    def record(progress: float) -> None:
        nonlocal last
        if progress - last >= 0.02 or progress >= 1.0:
            last = progress
            try:
                update_stage_progress(db, job_id, stage, round(progress, 3))
            except Exception as e:  # 進捗記録の失敗で文字起こしを止めない
                logger.debug(f"進捗記録失敗: {e}")

    return record


def run_post_stage(db: Path, job_id: int, stage: str, fn) -> bool:
    """後処理ステージを実行し結果を job_stages に記録する。

    fn が真を返せば done、偽なら skipped。例外は failed として記録して握りつぶす
    （文字起こし結果は完了済みのため）。成功したら True を返す。
    """
    try:
        return bool(run_stage(db, job_id, stage, fn))
    except Exception as e:
        logger.warning(f"[job {job_id}] {stage} 失敗（文字起こしは完了済み）: {e}")
        return False
