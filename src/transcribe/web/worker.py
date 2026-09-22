"""Web UI からのコマンドを 1 本のワーカースレッドで順番に実行するタスクキュー。

- CLI コマンド（typer）をサブプロセスではなくインプロセスで実行する
- 同時に実行されるタスクは常に 1 つ（GPU・DB の取り合いが起きない）
- Whisper モデルをタスク間で使い回し、一定時間アイドルが続いたら解放する
"""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import click

from .output_router import LineCapture

logger = logging.getLogger(__name__)

IDLE_RELEASE_SECONDS = 600


@dataclass
class Task:
    id: str
    args: list[str]
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue
    cleanup: Path | None = None
    lines: list[str] = field(default_factory=list)


class TaskWorker:
    def __init__(self, idle_release_seconds: int = IDLE_RELEASE_SECONDS) -> None:
        self._queue: queue.Queue[Task] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._idle_release_seconds = idle_release_seconds
        self._pending = 0
        self.current: Task | None = None

    def ensure_started(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            from ..pipeline import keep_model_loaded  # noqa: PLC0415

            keep_model_loaded(True)
            self._thread = threading.Thread(target=self._loop, name="transcribe-worker", daemon=True)
            self._thread.start()

    def pending_count(self) -> int:
        with self._lock:
            return self._pending

    def submit(self, task: Task) -> int:
        """タスクを投入し、先に待っているタスク数を返す。"""
        self.ensure_started()
        with self._lock:
            ahead = self._pending + (1 if self.current is not None else 0)
            self._pending += 1
        self._queue.put(task)
        return ahead

    # ─── worker thread ──────────────────────────────────────────────────

    def _loop(self) -> None:
        while True:
            try:
                task = self._queue.get(timeout=self._idle_release_seconds)
            except queue.Empty:
                self._release_models()
                continue
            with self._lock:
                self._pending -= 1
                self.current = task
            try:
                self._execute(task)
            finally:
                with self._lock:
                    self.current = None

    def _execute(self, task: Task) -> None:
        from ..cli import app as cli_app  # noqa: PLC0415
        import typer  # noqa: PLC0415

        exit_code: int | None = 0
        with LineCapture(lambda line: self._emit(task, line)):
            try:
                command = typer.main.get_command(cli_app)
                result = command.main(args=task.args, prog_name="transcribe", standalone_mode=False)
                exit_code = result if isinstance(result, int) else 0
            except click.exceptions.Exit as e:
                exit_code = e.exit_code
            except click.exceptions.Abort:
                print("中止しました")
                exit_code = 1
            except click.ClickException as e:
                print(f"エラー: {e.format_message()}")
                exit_code = e.exit_code
            except Exception:
                print(traceback.format_exc())
                exit_code = 1

        if task.cleanup is not None:
            task.cleanup.unlink(missing_ok=True)
        self._finish(task, f"[完了 (exit={exit_code})]")

    def _release_models(self) -> None:
        from ..pipeline import release_model_cache  # noqa: PLC0415

        try:
            release_model_cache()
        except Exception as e:  # pragma: no cover
            logger.warning(f"モデル解放に失敗: {e}")

    # ─── event loop への受け渡し ─────────────────────────────────────────

    def _emit(self, task: Task, line: str) -> None:
        from .runner import deliver  # noqa: PLC0415

        task.loop.call_soon_threadsafe(deliver, task, line)

    def _finish(self, task: Task, exit_msg: str) -> None:
        from .runner import finish  # noqa: PLC0415

        task.loop.call_soon_threadsafe(finish, task, exit_msg)


worker = TaskWorker()
