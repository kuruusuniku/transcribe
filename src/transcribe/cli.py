from __future__ import annotations

import logging
import shutil
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
    get_all_jobs,
    get_job_by_id,
    get_job_by_url_or_id,
    init_db,
    reset_for_retry,
    reset_for_rerun,
)
from .utils import ensure_dir

app = typer.Typer(name="transcribe", add_completion=False, help="YouTube動画 自動文字起こしツール")
console = Console()

_PROJECT_ROOT = Path(__file__).parent.parent.parent
_CONFIG_PATH = _PROJECT_ROOT / "config.yaml"
_GLOSSARY_PATH = _PROJECT_ROOT / "glossary.yaml"
_URLS_PATH = _PROJECT_ROOT / "urls.txt"


def _load_cfg_and_glossary():
    cfg = load_config(_CONFIG_PATH)
    glossary = load_glossary(_GLOSSARY_PATH)
    setup_logging(cfg.logging.level, cfg.log_dir, console=cfg.logging.console, file=cfg.logging.file)
    ensure_dir(cfg.work_dir)
    ensure_dir(cfg.output_dir)
    ensure_dir(cfg.log_dir)
    init_db(cfg.state_db)
    return cfg, glossary


@app.command()
def run(
    urls_file: Path = typer.Option(_URLS_PATH, "--urls", "-u", help="URLリストファイル"),
) -> None:
    """urls.txt を読んで未処理ジョブを文字起こしする"""
    cfg, glossary = _load_cfg_and_glossary()
    logger = logging.getLogger(__name__)

    if not urls_file.exists():
        console.print(f"[yellow]URLファイルが見つかりません: {urls_file}[/yellow]")
        raise typer.Exit(0)

    lines = urls_file.read_text(encoding="utf-8").splitlines()
    urls = [
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]

    if not urls:
        logger.info("処理対象URLなし")
        console.print("[yellow]urls.txt にURLが登録されていません。[/yellow]")
        raise typer.Exit(0)

    console.print(f"[green]{len(urls)} 件のURLを処理します[/green]")

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        run_pipeline(urls, cfg, glossary, progress=progress)


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
