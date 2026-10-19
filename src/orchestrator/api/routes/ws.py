"""WebSocket live logs: /ws/runs/{run_id}/logs

Streams existing stdout/stderr lines first, then follows file growth (so runs started by a
separate CLI process stream too), emitting status changes and a final `end` event.
Auth: when a token is configured, pass it as `?token=...` (browsers cannot set headers on
WebSocket upgrades).
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from orchestrator.core.db import session_scope
from orchestrator.core.models import Run
from orchestrator.core.security import token_ok
from orchestrator.core.state import TERMINAL_RUN_STATES
from orchestrator.services import artifact_service

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
