from __future__ import annotations

import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TimeElapsedColumn
from rich.table import Table

from . import setup_logging
from .config import load_config, load_glossary
from .pipeline import run_pipeline
from .state import (
    get_all_done_jobs,
    get_all_jobs,
    get_job_by_id,
    get_job_by_url_or_id,
    get_unsummarized_done_jobs,
    get_unsynced_done_jobs,
    init_db,
    record_summarized,
    record_synced,
    reset_for_retry,
    reset_for_rerun,
)
from .summarize import generate_summary
from .sync import sync_job
from .utils import ensure_dir

app = typer.Typer(name="transcribe", add_completion=False, help="YouTube動画・ローカル音声ファイル 自動文字起こしツール")
console = Console()

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_GLOSSARY_PATH = _PROJECT_ROOT / "glossary.yaml"
_URLS_PATH = _PROJECT_ROOT / "urls.txt"

SUPPORTED_AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".m4a"})


def _load_cfg_and_glossary():
    cfg = load_config(_CONFIG_PATH)
    glossary = load_glossary(_GLOSSARY_PATH)
    setup_logging(cfg.logging.level, cfg.log_dir, console=cfg.logging.console, file=cfg.logging.file)
    ensure_dir(cfg.work_dir)
    ensure_dir(cfg.output_dir)
    ensure_dir(cfg.log_dir)
    init_db(cfg.state_db)
    return cfg, glossary


def _validate_local_path(path_str: str) -> str:
    """ローカルファイルパスのバリデーション。絶対パスに正規化して返す。"""
    p = Path(path_str).resolve()
    if not p.exists():
        raise FileNotFoundError(f"ファイルが見つかりません: {path_str}")
    if p.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
        raise ValueError(f"対応していない拡張子です（対応: {allowed}）: {p.suffix}")
    return str(p)


@app.command()
def run(
    urls_file: Path = typer.Option(_URLS_PATH, "--urls", "-u", help="URLリストファイル"),
) -> None:
    """urls.txt を読んで未処理ジョブを文字起こしする（URLとローカルファイルパスの混在可）"""
    cfg, glossary = _load_cfg_and_glossary()
    logger = logging.getLogger(__name__)

    if not urls_file.exists():
        console.print(f"[yellow]URLファイルが見つかりません: {urls_file}[/yellow]")
        raise typer.Exit(0)

    lines = urls_file.read_text(encoding="utf-8").splitlines()
    raw_entries = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]

    if not raw_entries:
        logger.info("処理対象なし")
        console.print("[yellow]urls.txt にURLまたはファイルパスが登録されていません。[/yellow]")
        raise typer.Exit(0)

    entries: list[str] = []
    for entry in raw_entries:
        if entry.startswith(("http://", "https://")):
            entries.append(entry)
        else:
            try:
                entries.append(_validate_local_path(entry))
            except (FileNotFoundError, ValueError) as e:
                console.print(f"[red]{e}[/red]")
                logger.error(str(e))

    if not entries:
        console.print("[yellow]有効な処理対象がありません。[/yellow]")
        raise typer.Exit(0)

    console.print(f"[green]{len(entries)} 件を処理します[/green]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        run_pipeline(entries, cfg, glossary, progress=progress)


@app.command()
def file(
    path: Path = typer.Argument(..., help="音声ファイルパス（mp3 / m4a）"),
) -> None:
    """ローカル音声ファイル（mp3 / m4a）を文字起こしする"""
    cfg, glossary = _load_cfg_and_glossary()

    resolved = Path(path).resolve()
    if not resolved.exists():
        console.print(f"[red]ファイルが見つかりません: {path}[/red]")
        raise typer.Exit(1)
    if resolved.suffix.lower() not in SUPPORTED_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(SUPPORTED_AUDIO_EXTENSIONS))
        console.print(f"[red]対応していない拡張子です（対応: {allowed}）: {resolved.suffix}[/red]")
        raise typer.Exit(1)

    abs_path = str(resolved)
    console.print(f"[green]ファイルを処理します: {resolved.name}[/green]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        run_pipeline([abs_path], cfg, glossary, progress=progress)


@app.command()
def convert(
    path: Path = typer.Argument(..., help="変換元の音声ファイル（現在 .m4a のみ対応）"),
    force: bool = typer.Option(False, "--force", "-f", help="出力先 .mp3 が存在しても確認なしで上書き"),
) -> None:
    """音声ファイルを mp3 に変換する（faster-whisper 同梱の ffmpeg を使用）"""
    resolved = Path(path).resolve()
    if not resolved.exists():
        console.print(f"[red]ファイルが見つかりません: {path}[/red]")
        raise typer.Exit(1)

    ext = resolved.suffix.lower()
    if ext == ".mp3":
        console.print(f"[yellow]既に MP3 です: {resolved.name}[/yellow]")
        raise typer.Exit(0)
    if ext != ".m4a":
        console.print(f"[red]対応していない変換元拡張子です（対応: .m4a）: {resolved.suffix}[/red]")
        raise typer.Exit(1)

    dest = resolved.with_suffix(".mp3")
    if dest.exists() and not force:
        if not typer.confirm(f"{dest.name} が既に存在します。上書きしますか?"):
            console.print("[yellow]中止しました[/yellow]")
            raise typer.Exit(0)

    console.print(f"[green]変換中: {resolved.name} → {dest.name}[/green]")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(resolved),
                "-vn",
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(dest),
            ],
            check=True,
            capture_output=True,
        )
    except FileNotFoundError:
        console.print("[red]ffmpeg が見つかりません。PATH を確認してください。[/red]")
        raise typer.Exit(1)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
        console.print(f"[red]ffmpeg 変換に失敗しました（exit={e.returncode}）[/red]")
        if stderr:
            console.print(stderr)
        raise typer.Exit(1)

    console.print(f"[green]✓ 変換完了: {dest}[/green]")


@app.command()
def status() -> None:
    """全ジョブの状態を表示する"""
    cfg, _ = _load_cfg_and_glossary()
    jobs = get_all_jobs(cfg.state_db)

    if not jobs:
        console.print("[yellow]ジョブがありません[/yellow]")
        return

    table = Table(title="ジョブ一覧", show_lines=True)
    table.add_column("ID", style="dim")
    table.add_column("Status", style="bold")
    table.add_column("Retry")
    table.add_column("Title")
    table.add_column("URL", no_wrap=False)
    table.add_column("Updated")
    table.add_column("Output Dir")

    status_colors = {
        "done": "green",
        "failed": "red",
        "queued": "yellow",
        "transcribing": "cyan",
        "downloading": "blue",
        "separating": "magenta",
        "formatting": "cyan",
    }

    for job in jobs:
        color = status_colors.get(job["status"], "white")
        out_dir = Path(job["output_dir"]).name if job["output_dir"] else "-"
        table.add_row(
            str(job["id"]),
            f"[{color}]{job['status']}[/{color}]",
            str(job["retry_count"]),
            job["title"] or "-",
            job["url"],
            (job["updated_at"] or "")[:16],
            out_dir,
        )

    console.print(table)


@app.command()
def retry(job_id: int = typer.Argument(..., help="再実行するジョブID")) -> None:
    """失敗したジョブを手動で再投入する"""
    cfg, glossary = _load_cfg_and_glossary()
    logger = logging.getLogger(__name__)

    job = get_job_by_id(cfg.state_db, job_id)
    if job is None:
        console.print(f"[red]ジョブ {job_id} が見つかりません[/red]")
        raise typer.Exit(1)

    reset_for_retry(cfg.state_db, job_id)
    logger.info(f"ジョブ {job_id} をリトライキューに戻しました")
    console.print(f"[green]ジョブ {job_id} をリトライします[/green]")

    run_pipeline([job["url"]], cfg, glossary)


@app.command()
def rerun(
    url_or_id: str = typer.Argument(..., help="URL または video_id"),
    no_backup: bool = typer.Option(False, "--no-backup", help="バックアップせず既存出力を削除"),
    yes: bool = typer.Option(False, "--yes", "-y", help="確認プロンプトをスキップ"),
) -> None:
    """指定ジョブを最初から再処理する（検証用）"""
    cfg, glossary = _load_cfg_and_glossary()
    db = cfg.state_db

    job = get_job_by_url_or_id(db, url_or_id)

    if job is None:
        console.print(
            f"[yellow]該当ジョブが見つかりません。新規ジョブとして登録します: {url_or_id}[/yellow]"
        )
        run_pipeline([url_or_id], cfg, glossary)
        return

    source_type = job["source_type"] if "source_type" in job.keys() else "youtube"
    if source_type == "local" and not Path(job["url"]).exists():
        console.print(
            f"[yellow]WARNING: ソースファイルが見つかりません: {job['url']}（スキップ）[/yellow]"
        )
        logging.getLogger(__name__).warning(f"Source file not found: {job['url']}, skipping job {job['id']}")
        raise typer.Exit(0)

    output_dir = Path(job["output_dir"]) if job["output_dir"] else None
    dir_exists = output_dir is not None and output_dir.exists()

    backup_dir: Path | None = None
    if dir_exists and not no_backup:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = output_dir.parent / f"{output_dir.name}_backup_{timestamp}"

    if not yes:
        if dir_exists:
            action = (
                "削除されます"
                if no_backup
                else f"[cyan]{backup_dir}[/cyan] にリネームされます"
            )
            console.print(
                f"このジョブを再実行します。\n"
                f"既存の出力ディレクトリは {action}。"
            )
        else:
            console.print("このジョブを再実行します。")
        typer.confirm("続行しますか?", abort=True)

    if dir_exists:
        if no_backup:
            shutil.rmtree(output_dir)
            console.print(f"削除: {output_dir}")
        else:
            output_dir.rename(backup_dir)
            console.print(f"バックアップ: {output_dir.name} → {backup_dir.name}")
    elif output_dir is not None:
        console.print(f"[yellow]出力ディレクトリが見つかりません（スキップ）: {output_dir}[/yellow]")

    reset_for_rerun(db, job["id"])
    console.print(f"[green]ジョブ {job['id']} ({job['url']}) を再実行します[/green]")
    run_pipeline([job["url"]], cfg, glossary)


@app.command()
def sync(
    all_jobs: bool = typer.Option(False, "--all", help="done ジョブを全件再同期（synced_at を無視）"),
) -> None:
    """未同期の完了済みジョブを Google Docs に同期する"""
    cfg, _ = _load_cfg_and_glossary()
    logger = logging.getLogger(__name__)

    if not cfg.google_docs.enabled:
        console.print(
            "[yellow]Google Docs 同期が無効です。[/yellow]\n"
            "config.yaml で google_docs.enabled を true に設定してください。"
        )
        raise typer.Exit(0)

    if not cfg.google_docs.root_folder_id:
        console.print(
            "[yellow]Google Drive のルートフォルダ ID が未設定です。[/yellow]\n"
            "config.yaml で google_docs.root_folder_id を設定してください。"
        )
        raise typer.Exit(0)

    jobs = get_all_done_jobs(cfg.state_db) if all_jobs else get_unsynced_done_jobs(cfg.state_db)

    if not jobs:
        console.print("[yellow]同期対象のジョブがありません[/yellow]")
        raise typer.Exit(0)

    console.print(f"[green]{len(jobs)} 件を同期します[/green]")

    success_count = 0
    skip_count = 0
    fail_count = 0

    for job in jobs:
        job_id = job["id"]
        output_dir = Path(job["output_dir"]) if job["output_dir"] else None
        if output_dir is None or not output_dir.exists():
            logger.warning(f"[job {job_id}] 出力ディレクトリなし、スキップ")
            skip_count += 1
            continue
        try:
            doc_id = sync_job(dict(job), output_dir, cfg.google_docs)
            if doc_id:
                record_synced(cfg.state_db, job_id)
                console.print(f"  [green]✓[/green] job {job_id}: {job['title'] or job['url']}")
                success_count += 1
            else:
                skip_count += 1
        except Exception as e:
            logger.error(f"[job {job_id}] 同期失敗: {e}")
            console.print(f"  [red]✗[/red] job {job_id}: {e}")
            fail_count += 1

    console.print(f"\n成功: {success_count}  スキップ: {skip_count}  失敗: {fail_count}")


@app.command()
def summarize(
    all_jobs: bool = typer.Option(False, "--all", help="done ジョブを全件再まとめ（summarized_at を無視）"),
    job_id: int | None = typer.Option(None, "--id", help="特定ジョブIDのみ処理"),
) -> None:
    """完了済みジョブに対して LLM でカスタムまとめを生成する"""
    cfg, glossary = _load_cfg_and_glossary()
    logger = logging.getLogger(__name__)

    if not cfg.summarize.enabled:
        console.print(
            "[yellow]まとめ生成が無効です。[/yellow]\n"
            "config.yaml で summarize.enabled を true に設定してください。"
        )
        raise typer.Exit(0)

    api_key = cfg.summarize.resolve_api_key()
    if not api_key:
        provider = cfg.summarize.provider
        env_var = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
        cfg_key = "gemini_api_key" if provider == "gemini" else "anthropic_api_key"
        console.print(
            f"[yellow]{provider} の API キーが設定されていません。[/yellow]\n"
            f"config.yaml の summarize.{cfg_key} または 環境変数 {env_var} を設定してください。"
        )
        raise typer.Exit(1)

    if job_id is not None:
        job = get_job_by_id(cfg.state_db, job_id)
        if job is None:
            console.print(f"[red]ジョブ {job_id} が見つかりません[/red]")
            raise typer.Exit(1)
        if job["status"] != "done":
            console.print(f"[yellow]ジョブ {job_id} は done 状態ではありません: {job['status']}[/yellow]")
            raise typer.Exit(0)
        jobs = [job]
    elif all_jobs:
        jobs = get_all_done_jobs(cfg.state_db)
    else:
        jobs = get_unsummarized_done_jobs(cfg.state_db)

    if not jobs:
        console.print("[yellow]まとめ対象のジョブがありません[/yellow]")
        raise typer.Exit(0)

    console.print(f"[green]{len(jobs)} 件をまとめます[/green]")

    success_count = 0
    skip_count = 0
    fail_count = 0

    for job in jobs:
        jid = job["id"]
        output_dir = Path(job["output_dir"]) if job["output_dir"] else None
        if output_dir is None or not output_dir.exists():
            logger.warning(f"[job {jid}] 出力ディレクトリなし、スキップ")
            skip_count += 1
            continue

        source_type = job["source_type"] if "source_type" in job.keys() else "youtube"
        try:
            summary_path = generate_summary(
                output_dir=output_dir,
                video_url=job["url"],
                video_title=job["title"] or "",
                cfg=cfg.summarize,
                glossary_entries=glossary.substitutions,
                source_type=source_type,
            )
            if summary_path:
                record_summarized(cfg.state_db, jid)
                console.print(f"  [green]✓[/green] job {jid}: {job['title'] or job['url']}")
                success_count += 1
            else:
                skip_count += 1
        except Exception as e:
            logger.error(f"[job {jid}] まとめ失敗: {e}")
            console.print(f"  [red]✗[/red] job {jid}: {e}")
            fail_count += 1

    console.print(f"\n成功: {success_count}  スキップ: {skip_count}  失敗: {fail_count}")


@app.command()
def web(
    host: str = typer.Option("", "--host", help="ホストアドレス（空の場合は config.yaml の値を使用）"),
    port: int = typer.Option(0, "--port", "-p", help="ポート番号（0 の場合は config.yaml の値を使用）"),
    reload: bool = typer.Option(False, "--reload", help="開発時のホットリロード"),
) -> None:
    """Web UI サーバーを起動する（http://localhost:8000）"""
    import uvicorn
    from .web.app import app as web_app

    cfg, _ = _load_cfg_and_glossary()
    actual_host = host or cfg.web.host
    actual_port = port or cfg.web.port

    console.print(f"[green]transcribe Web UI を起動します: http://{actual_host}:{actual_port}[/green]")
    uvicorn.run(web_app, host=actual_host, port=actual_port, reload=reload)


@app.command()
def clean() -> None:
    """data/work の一時ファイルを削除する"""
    cfg, _ = _load_cfg_and_glossary()
    work_dir = cfg.work_dir

    files = list(work_dir.glob("*"))
    if not files:
        console.print("[yellow]削除対象ファイルなし[/yellow]")
        return

    for f in files:
        if f.is_file():
            f.unlink()
            console.print(f"削除: {f.name}")

    console.print(f"[green]{len(files)} ファイルを削除しました[/green]")
