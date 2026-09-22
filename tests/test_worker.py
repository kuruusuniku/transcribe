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


def test_describe_command_labels():
    from transcribe.web.worker import describe_command

    assert describe_command(["file", "C:/a/講義.mp3"]) == "文字起こし: 講義.mp3"
    assert describe_command(["retry", "12"]) == "再開: ジョブ #12"
    assert describe_command(["resume-post", "7"]) == "後処理のやり直し: ジョブ #7"
    assert describe_command(["sync-notion", "--all"]) == "Notion 登録（全件）"
    assert describe_command(["sync-notion", "--id", "52"]) == "Notion 登録: ジョブ #52"
    assert describe_command(["sync", "--id", "52"]) == "Google Docs 登録: ジョブ #52"
    assert describe_command(["summarize", "--id", "52"]) == "まとめ生成: ジョブ #52"
    assert describe_command(["summarize"]) == "まとめ生成（未処理分）"
    assert describe_command(["clean"]) == "一時ファイルの削除"


def test_cancel_pending_task():
    async def submit():
        return [start_task(["--help"]), start_task(["status"]), start_task(["status"])]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        from transcribe.web.worker import worker

        ids = loop.run_until_complete(submit())
        # 2 番目以降は待機中のはずなので取り消せる（1 番目は実行中の可能性がある）
        cancelled = [t for t in ids[1:] if worker.cancel(t)]
        assert cancelled, "待機中のタスクを取り消せなかった"

        async def drain():
            for tid in ids:
                q = active_tasks.get(tid)
                if q is None:
                    continue
                lines = []
                while (line := await asyncio.wait_for(q.get(), 30)) is not None:
                    lines.append(line)
                if tid in cancelled:
                    assert "[取り消し]" in lines[-1]

        loop.run_until_complete(drain())
        assert worker.cancel("no-such-task") is False
    finally:
        loop.close()
