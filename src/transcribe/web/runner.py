from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

PROJECT_ROOT = Path(__file__).parent.parent.parent.parent

PYTHON = sys.executable

# task_id -> asyncio.Queue[str | None]
active_tasks: dict[str, asyncio.Queue] = {}

# queues for global log subscribers
global_subscribers: set[asyncio.Queue] = set()


async def _run(task_id: str, cmd: list[str], cleanup: Path | None = None) -> None:
    q: asyncio.Queue[str | None] = asyncio.Queue()
    active_tasks[task_id] = q

    exit_msg = "[完了]"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
            cwd=str(PROJECT_ROOT),
        )

        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip("\r\n")
            await q.put(text)
            _broadcast(task_id, text)

        await proc.wait()
        exit_msg = f"[完了 (exit={proc.returncode})]"
    except Exception as e:
        exit_msg = f"[ERROR] {e}"

    await q.put(exit_msg)
    await q.put(None)
    _broadcast(task_id, exit_msg)

    active_tasks.pop(task_id, None)

    if cleanup is not None:
        try:
            cleanup.unlink(missing_ok=True)
        except OSError:
            pass


def _broadcast(task_id: str, text: str) -> None:
    msg = f"[{task_id[:8]}] {text}"
    for gq in list(global_subscribers):
        try:
            gq.put_nowait(msg)
        except asyncio.QueueFull:
            pass


def start_task(cmd: list[str], cleanup: Path | None = None) -> str:
    task_id = str(uuid4())
    asyncio.create_task(_run(task_id, cmd, cleanup))
    return task_id


def transcribe_cmd(*args: str) -> list[str]:
    return [PYTHON, "-m", "transcribe", *args]
