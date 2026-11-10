"""Shared memory endpoints (frozen contract per the product spec)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.core.schemas import MemoryCreate, MemoryUpdate
from orchestrator.services import memory_service, memory_triage

router = APIRouter(dependencies=AUTH)


def dump(obj: Any) -> dict[str, Any]:
    return json.loads(obj.model_dump_json())


def eviction_response(e: memory_service.EvictionRefused) -> JSONResponse:
    """The frozen error body, with the casualty list as STRUCTURE in `detail`.

    Both GUIs act on `detail.evicted` (ids, bucket, scope, roles, self) instead of matching
    a substring of the message, so rewording the prose can never break the override flow
    (T-17 review round 3, finding 4).
    """
    return JSONResponse(status_code=400, content={"error": str(e), "detail": e.payload})


@router.get("/memory")
def list_memory(
    category: str | None = None,
    scope: str | None = None,
    role: str | None = None,
    project_slug: str | None = None,
    status: str | None = Query(None, pattern="^(approved|pending|rejected)$"),
    session: Session = SessionDep,
) -> list[dict[str, Any]]:
    return [
        dump(e)
        for e in memory_service.list_entries(
            session,
            category=category,
            scope=scope,
            role=role,
            project_slug=project_slug,
            status=status,
        )
    ]


@router.post("/memory", status_code=201, response_model=None)
def create_memory(
    body: MemoryCreate, override: bool = False, session: Session = SessionDep
) -> dict[str, Any] | JSONResponse:
    # Out-of-vocabulary scopes are refused here for EVERY category (T-4 DoD 2); the message
    # names the allowed values. The entry shape stays exactly as frozen — the marker for an
    # unreachable scope is shown by `orchestrator memory list`, not by a new response field.
    # `approved=true` is an approval, so it goes through the same eviction refusal as
    # `POST /memory/{id}/approve`; `override=true` is the explicit opt-in.
    try:
        return dump(memory_service.create(session, body, override=override))
    except memory_service.EvictionRefused as e:
        return eviction_response(e)
    except memory_service.MemoryError_ as e:
        raise HTTPException(400, str(e)) from e


# Declared before /memory/{entry_id} routes so the literal paths win.
@router.get("/memory/search")
def search_memory(
    q: str = Query(..., min_length=1), session: Session = SessionDep
) -> list[dict[str, Any]]:
    return [dump(e) for e in memory_service.search(session, q)]


@router.get("/memory/triage")
def triage_memory(
    status: str = Query("pending", pattern="^(approved|pending|rejected)$"),
    project: str | None = None,
    role: str = Query("worker", pattern="^(architect|supervisor|worker|reviewer|agent)$"),
    session: Session = SessionDep,
) -> dict[str, Any]:
    """Read-only triage report: rot flags, live staleness claims, merge/amend groups, cost.

    Additive and non-mutating; approving and rejecting stay separate explicit calls.
    """
    return memory_triage.triage(session, status=status, project_slug=project, role=role)


@router.get("/memory/budget")
def memory_budget(project: str | None = None, session: Session = SessionDep) -> dict[str, Any]:
    """Static cost of injected memory per role, caps, and what the caps demote."""
    return memory_triage.budget_report(session, project)


@router.get("/memory/export")
def export_memory(session: Session = SessionDep) -> dict[str, Any]:
    return memory_service.export_all(session)


@router.post("/memory/import", response_model=None)
async def import_memory(
    request: Request, override: bool = False, session: Session = SessionDep
) -> dict[str, Any] | JSONResponse:
    try:
        payload = await request.json()
    except ValueError as e:
        raise HTTPException(400, "body must be a JSON memory export") from e
    try:
        return {"imported": memory_service.import_all(session, payload, override=override)}
    except memory_service.EvictionRefused as e:
        return eviction_response(e)
    except memory_service.MemoryError_ as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/memory/{entry_id}")
def update_memory(
    entry_id: int, body: MemoryUpdate, session: Session = SessionDep
) -> dict[str, Any]:
    # A PATCH that would move an entry to a scope no injector can read is a 400, not a
    # silently unreachable row.
    try:
        return dump(memory_service.update(session, entry_id, body))
    except memory_service.MemoryError_ as e:
        raise HTTPException(400, str(e)) from e


@router.post("/memory/{entry_id}/approve", response_model=None)
def approve_memory(
    entry_id: int, override: bool = False, session: Session = SessionDep
) -> dict[str, Any] | JSONResponse:
    # An approval that would evict another approved row (or leave the row itself out of the
    # injected section) is refused (400); the casualty list is in `detail` and `override=true`
    # is the explicit opt-in. The route shape is unchanged and the query parameter is additive.
    try:
        return dump(memory_service.approve(session, entry_id, override=override))
    except memory_service.EvictionRefused as e:
        return eviction_response(e)
    except memory_service.MemoryError_ as e:
        raise HTTPException(400, str(e)) from e


@router.post("/memory/{entry_id}/reject")
def reject_memory(entry_id: int, session: Session = SessionDep) -> dict[str, Any]:
    return dump(memory_service.reject(session, entry_id))
