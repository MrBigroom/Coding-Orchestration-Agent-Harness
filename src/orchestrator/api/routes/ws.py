"""WebSocket channels: live run logs and interactive chat sessions.

/ws/runs/{run_id}/logs streams existing stdout/stderr lines first, then follows file growth
(so runs started by a separate CLI process stream too), emitting status changes and a final
`end` event.
Auth: when a token is configured, pass it as `?token=...` (browsers cannot set headers on
WebSocket upgrades).

/ws/chat/{chat_id} replays a session's transcript as one `history` message, then carries
live events out and prompts, approvals and control changes in.

SameOriginMiddleware refuses other origins on both channels.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from orchestrator.adapters.chat.base import DriverError
from orchestrator.core.db import session_scope
from orchestrator.core.models import ChatSession, Run
from orchestrator.core.security import token_ok
from orchestrator.core.state import TERMINAL_RUN_STATES
from orchestrator.services import artifact_service, chat_service

router = APIRouter()
POLL = 0.5


@router.websocket("/ws/runs/{run_id}/logs")
async def run_logs(ws: WebSocket, run_id: int) -> None:
    if not token_ok(ws.query_params.get("token")):
        await ws.close(code=4401)
        return
    with session_scope() as s:
        run = s.get(Run, run_id)
        if not run:
            await ws.close(code=4404)
            return
    await ws.accept()
    offsets = {"stdout": 0, "stderr": 0}
    partial = {"stdout": "", "stderr": ""}
    last_status = None
    try:
        while True:
            d = artifact_service.run_dir(run_id)
            for stream in ("stdout", "stderr"):
                path = d / f"{stream}.log"
                if not path.is_file():
                    continue
                with path.open("rb") as fh:
                    fh.seek(offsets[stream])
                    chunk = fh.read(256_000)
                if chunk:
                    offsets[stream] += len(chunk)
                    text = partial[stream] + chunk.decode(errors="replace")
                    *lines, partial[stream] = text.split("\n")
                    for line in lines:
                        await ws.send_json(
                            {"type": "log", "run_id": run_id, "stream": stream, "line": line}
                        )
            with session_scope() as s:
                run = s.get(Run, run_id)
                status = run.status if run else "unknown"
            if status != last_status:
                last_status = status
                await ws.send_json({"type": "status", "run_id": run_id, "status": status})
            if status in TERMINAL_RUN_STATES:
                await ws.send_json({"type": "end", "run_id": run_id, "status": status})
                await ws.close()
                return
            await asyncio.sleep(POLL)
    except WebSocketDisconnect:
        return


async def _chat_outbound(ws: WebSocket, queue: asyncio.Queue) -> None:
    with suppress(WebSocketDisconnect, RuntimeError):  # the browser went away
        while (event := await queue.get()) is not None:
            await ws.send_json(event)
        await ws.send_json({"type": "end"})


async def _chat_inbound(ws: WebSocket, live: chat_service.LiveSession) -> None:
    with suppress(WebSocketDisconnect, RuntimeError):  # the browser went away
        while True:
            message = await ws.receive_json()
            try:
                with session_scope() as s:
                    await chat_service.handle(s, live, message)
            except (DriverError, chat_service.ChatError) as e:
                await ws.send_json({"type": "error", "message": str(e)})


@router.websocket("/ws/chat/{chat_id}")
async def chat_socket(ws: WebSocket, chat_id: int) -> None:
    if not token_ok(ws.query_params.get("token")):
        await ws.close(code=4401)
        return
    with session_scope() as s:
        chat = s.get(ChatSession, chat_id)
        if not chat:
            await ws.close(code=4404)
            return
        status = chat.status
    await ws.accept()
    live = chat_service.manager.get(chat_id)
    queue = live.subscribe() if live else None  # before the replay, so nothing is missed
    await ws.send_json(
        {"type": "history", "events": chat_service.history(chat_id), "status": status}
    )
    if not live or not queue:
        await ws.send_json({"type": "end"})
        await ws.close()
        return
    tasks = [
        asyncio.create_task(_chat_outbound(ws, queue)),
        asyncio.create_task(_chat_inbound(ws, live)),
    ]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # No await here: this also runs while the connection itself is being cancelled.
        live.unsubscribe(queue)
        for task in tasks:
            task.cancel()
