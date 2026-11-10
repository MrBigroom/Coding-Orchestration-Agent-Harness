"""Supabase synchronization status and manually triggered pull/push cycles."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import Session

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.services import sync_service
from orchestrator.services.sync_runner import SyncBusyError, sync_runner
from orchestrator.services.sync_transport import SyncError

router = APIRouter()


@router.get("/sync/status", dependencies=AUTH)
def get_sync_status(session: Session = SessionDep) -> dict[str, Any]:
    return {**sync_runner.status(session), **sync_service.status(session)}


@router.post("/sync/run", dependencies=AUTH)
async def run_sync() -> Any:
    # Manual runs intentionally do not require automatic synchronization to be enabled.
    try:
        return await sync_runner.run_once()
    except SyncBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    except SyncError as exc:
        # The frozen error body carries structure, not prose: a caller (GUI or script) can act
        # on `detail.kind` / `detail.recovery` without substring-matching the sentence (T-41).
        detail = getattr(exc, "detail", None) or {"kind": "failed"}
        return JSONResponse(
            status_code=400, content={"error": str(exc), "detail": detail}
        )
