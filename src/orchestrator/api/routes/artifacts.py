"""Run artifacts and log polling. Paths are confined to ORCHESTRATOR_HOME/runs/<run_id>."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import PlainTextResponse
from sqlmodel import Session

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.core.models import Run
from orchestrator.services import artifact_service
from orchestrator.utils.paths import safe_join

router = APIRouter(dependencies=AUTH)
MAX_ARTIFACT_BYTES = 2_000_000


def _run(session: Session, run_id: int) -> Run:
    run = session.get(Run, run_id)
    if not run:
        raise HTTPException(404, "run not found")
    return run


@router.get("/artifacts/{run_id}")
def list_artifacts(run_id: int, session: Session = SessionDep) -> list[dict[str, Any]]:
    _run(session, run_id)
    return artifact_service.list_artifacts(run_id)


@router.get("/artifacts/{run_id}/{name:path}", response_class=PlainTextResponse)
def get_artifact(run_id: int, name: str, session: Session = SessionDep) -> PlainTextResponse:
    _run(session, run_id)
    path = safe_join(artifact_service.run_dir(run_id), name)  # raises UnsafePathError -> 400
    if not path.is_file():
        raise HTTPException(404, "artifact not found")
    data = path.read_bytes()[:MAX_ARTIFACT_BYTES]
    media = "application/json" if path.suffix == ".json" else "text/plain"
    return PlainTextResponse(data.decode(errors="replace"), media_type=media)


@router.get("/logs/{run_id}")
def poll_logs(
    run_id: int,
    stream: str = Query("stdout", pattern="^(stdout|stderr)$"),
    offset: int = Query(0, ge=0),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """Polling fallback for the WebSocket: returns bytes after `offset`."""
    run = _run(session, run_id)
    path = safe_join(artifact_service.run_dir(run_id), f"{stream}.log")
    if not path.is_file():
        return {"data": "", "offset": offset, "status": run.status}
    with path.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read(256_000)
    return {
        "data": chunk.decode(errors="replace"),
        "offset": offset + len(chunk),
        "status": run.status,
    }
