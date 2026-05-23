from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..runner import active_tasks, global_subscribers

router = APIRouter()


@router.websocket("/ws/logs/{task_id}")
async def ws_logs(websocket: WebSocket, task_id: str):
    await websocket.accept()

    q = active_tasks.get(task_id)
    if q is None:
        await websocket.send_text("[タスクが見つかりません]")
        await websocket.close()
        return

    try:
        while True:
            line = await q.get()
            if line is None:
                await websocket.send_text("[完了]")
                break
            await websocket.send_text(line)
    except WebSocketDisconnect:
        pass
    finally:
        await websocket.close()


@router.websocket("/ws/logs/global")
async def ws_global(websocket: WebSocket):
    await websocket.accept()
    gq: asyncio.Queue[str | None] = asyncio.Queue(maxsize=1000)
    global_subscribers.add(gq)

    try:
        while True:
            msg = await gq.get()
            if msg is None:
                break
            await websocket.send_text(msg)
    except WebSocketDisconnect:
        pass
    finally:
        global_subscribers.discard(gq)
        await websocket.close()
