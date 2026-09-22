from __future__ import annotations

import logging
import logging.handlers
from datetime import date
from pathlib import Path

from rich.logging import RichHandler


def _purge_old_logs(log_dir: Path, retention_days: int) -> None:
    """保存期間を過ぎたログファイルを削除する。"""
    if retention_days <= 0:
        return
    import time  # noqa: PLC0415

    threshold = time.time() - retention_days * 86400
    for f in log_dir.glob("*.log"):
        try:
            if f.stat().st_mtime < threshold:
                f.unlink()
        except OSError:
            pass


def setup_logging(
    level: str,
    log_dir: Path,
    *,
    console: bool = True,
    file: bool = True,
    retention_days: int = 30,
) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(log_level)

    # Web サーバーでは同一プロセスでコマンドを繰り返し実行するため、前回追加したハンドラを外してから付け直す
    for h in list(root.handlers):
        if getattr(h, "_transcribe_handler", False):
            root.removeHandler(h)
            h.close()

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    if console:
        rich_handler = RichHandler(rich_tracebacks=True, show_path=False)
        rich_handler.setLevel(log_level)
        rich_handler._transcribe_handler = True  # type: ignore[attr-defined]
        root.addHandler(rich_handler)

    if file:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / f"{date.today()}.log"
        fh = logging.FileHandler(log_file, encoding="utf-8")
        fh.setLevel(log_level)
        fh.setFormatter(fmt)
        fh._transcribe_handler = True  # type: ignore[attr-defined]
        _purge_old_logs(log_dir, retention_days)
        root.addHandler(fh)
