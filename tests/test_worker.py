from __future__ import annotations

import asyncio

from transcribe.web.output_router import LineCapture
from transcribe.web.runner import active_tasks, start_task


def test_line_capture_splits_lines_and_strips_ansi():
    lines: list[str] = []
    cap = LineCapture(lines.append)
    with cap:
        cap._write("\x1b[32mhello\x1b[0m\nwor")
        cap._write("ld\r 50%\r100%\n\n")
        cap._write("tail")
    assert lines == ["hello", "world", " 50%", "100%", "tail"]


def _collect(task_ids: list[str]) -> list[list[str]]:
    async def run():
        results = []
        for tid in task_ids:
            q = active_tasks[tid]
            lines = []
            while (line := await asyncio.wait_for(q.get(), 30)) is not None:
                lines.append(line)
            results.append(lines)
        return results

    return asyncio.get_event_loop().run_until_complete(run())


def test_worker_runs_cli_in_process_sequentially():
    async def submit():
        return [start_task(["--help"]), start_task(["no-such-command"])]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        ids = loop.run_until_complete(submit())
        help_lines, bad_lines = _collect(ids)
    finally:
        loop.close()

    assert help_lines[-1] == "[完了 (exit=0)]"
    # 出力ルーター未導入（テスト環境）のため、コマンドの出力行は届かず完了行のみ
    assert bad_lines[-1] == "[完了 (exit=2)]"
