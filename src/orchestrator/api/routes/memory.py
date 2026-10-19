"""Shared memory endpoints (frozen contract per the product spec)."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from sqlmodel import Session

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.core.schemas import MemoryCreate, MemoryUpdate
from orchestrator.services import memory_service

router = APIRouter(dependencies=AUTH)


def dump(obj: Any) -> dict[str, Any]:
    return json.loads(obj.model_dump_json())


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


@router.post("/memory", status_code=201)
def create_memory(body: MemoryCreate, session: Session = SessionDep) -> dict[str, Any]:
    try:
        return dump(memory_service.create(session, body))
    except memory_service.MemoryError_ as e:
        raise HTTPException(400, str(e)) from e


# Declared before /memory/{entry_id} routes so the literal paths win.
@router.get("/memory/search")
def search_memory(
    q: str = Query(..., min_length=1), session: Session = SessionDep
) -> list[dict[str, Any]]:
    return [dump(e) for e in memory_service.search(session, q)]


@router.get("/memory/export")
def export_memory(session: Session = SessionDep) -> dict[str, Any]:
    return memory_service.export_all(session)


@router.post("/memory/import")
async def import_memory(request: Request, session: Session = SessionDep) -> dict[str, Any]:
    try:
        payload = await request.json()
    except ValueError as e:
        raise HTTPException(400, "body must be a JSON memory export") from e
    try:
        return {"imported": memory_service.import_all(session, payload)}
    except memory_service.MemoryError_ as e:
        raise HTTPException(400, str(e)) from e


@router.patch("/memory/{entry_id}")
def update_memory(
    entry_id: int, body: MemoryUpdate, session: Session = SessionDep
) -> dict[str, Any]:
    return dump(memory_service.update(session, entry_id, body))


@router.post("/memory/{entry_id}/approve")
def approve_memory(entry_id: int, session: Session = SessionDep) -> dict[str, Any]:
    return dump(memory_service.approve(session, entry_id))


@router.post("/memory/{entry_id}/reject")
def reject_memory(entry_id: int, session: Session = SessionDep) -> dict[str, Any]:
    return dump(memory_service.reject(session, entry_id))
