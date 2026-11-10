"""Supabase synchronization status and manually triggered pull/push cycles."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from sqlmodel import Session

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.services import sync_service
from orchestrator.services.sync_runner import SyncBusyError, sync_runner
from orchestrator.services.sync_transport import (
    SyncError,
    SyncPermanentError,
    SyncTransientError,
)

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
        # A transient upstream/network failure is 503 (retryable), not 400: a 400 tells the
        # operator the request itself was wrong and sends them to the wrong repair path.
        detail = getattr(exc, "detail", None) or {"kind": "failed"}
        if isinstance(exc, SyncPermanentError):
            status_code = 400
        elif isinstance(exc, SyncTransientError):
            status_code = 503
        else:
            status_code = 400
        return JSONResponse(
            status_code=status_code, content={"error": str(exc), "detail": detail}
        )
