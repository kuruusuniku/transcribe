from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

from .worker import Task, worker

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

# task_id -> asyncio.Queue[str | None]
active_tasks: dict[str, asyncio.Queue] = {}

FINISHED_TASK_TTL_SECONDS = 300

# queues for global log subscribers
global_subscribers: set[asyncio.Queue] = set()


def deliver(task: Task, text: str) -> None:
    """（イベントループ上で実行）タスクのログ 1 行を購読者に配信する。"""
    task.queue.put_nowait(text)
    _broadcast(task.id, text)


def finish(task: Task, exit_msg: str) -> None:
    """（イベントループ上で実行）タスク完了を通知し、ストリームを閉じる。"""
    task.queue.put_nowait(exit_msg)
    task.queue.put_nowait(None)
    _broadcast(task.id, exit_msg)
    # 完了直後に WebSocket が接続しても取りこぼさないよう、しばらくキューを残す
    task.loop.call_later(FINISHED_TASK_TTL_SECONDS, active_tasks.pop, task.id, None)


def _broadcast(task_id: str, text: str) -> None:
    msg = f"[{task_id[:8]}] {text}"
    for gq in list(global_subscribers):
        try:
            gq.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def start_task(cmd: list[str], cleanup: Path | None = None) -> str:
    """CLI コマンド（引数リスト）をワーカーのキューに投入し task_id を返す。

    イベントループ上から呼ぶこと。
    """
    task_id = str(uuid4())
    q: asyncio.Queue[str | None] = asyncio.Queue()
    active_tasks[task_id] = q
    task = Task(id=task_id, args=cmd, loop=asyncio.get_running_loop(), queue=q, cleanup=cleanup)
    ahead = worker.submit(task)
    if ahead:
        deliver(task, f"[待機中] 先行タスク {ahead} 件の完了後に実行します")
    return task_id


def transcribe_cmd(*args: str) -> list[str]:
    return list(args)
