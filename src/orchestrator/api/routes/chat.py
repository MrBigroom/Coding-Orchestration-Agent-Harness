"""Interactive harness sessions. The live channel is /ws/chat/{id} in routes/ws.py."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from sqlmodel import Session

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.core.schemas import ChatSessionCreate
from orchestrator.services import chat_service, standalone_service

router = APIRouter(dependencies=AUTH)


def dump(chat: Any) -> dict[str, Any]:
    return json.loads(chat.model_dump_json())


@router.get("/chat/harnesses")
def harnesses(session: Session = SessionDep) -> dict[str, Any]:
    return {**chat_service.harnesses(session), "agents": standalone_service.chat_choices(session)}


@router.get("/chat/sessions")
def list_sessions(session: Session = SessionDep) -> list[dict[str, Any]]:
    return [dump(c) for c in chat_service.list_sessions(session)]


@router.post("/chat/sessions", status_code=201)
async def create_session(body: ChatSessionCreate, session: Session = SessionDep) -> dict[str, Any]:
    try:
        return dump(await chat_service.create(session, body))
    except chat_service.ChatDisabled as e:
        raise HTTPException(403, str(e)) from e


@router.get("/chat/sessions/{chat_id}")
def get_session(chat_id: int, session: Session = SessionDep) -> dict[str, Any]:
    return dump(chat_service.get(session, chat_id))


@router.post("/chat/sessions/{chat_id}/close")
async def close_session(chat_id: int, session: Session = SessionDep) -> dict[str, Any]:
    await chat_service.close(chat_id)
    session.expire_all()
    return dump(chat_service.get(session, chat_id))


@router.post("/chat/sessions/{chat_id}/continue")
async def continue_session(chat_id: int, session: Session = SessionDep) -> dict[str, Any]:
    try:
        return dump(await chat_service.continue_session(session, chat_id))
    except chat_service.ChatDisabled as e:
        raise HTTPException(403, str(e)) from e


@router.get("/chat/sessions/{chat_id}/images/{name}")
def image(chat_id: int, name: str) -> FileResponse:
    path = chat_service.image_path(chat_id, name)
    if path is None:
        raise HTTPException(404, "image not found")
    return FileResponse(path)


@router.get("/chat/sessions/{chat_id}/changes")
def changes(chat_id: int, session: Session = SessionDep) -> dict[str, Any]:
    return chat_service.changes(session, chat_service.get(session, chat_id))


@router.post("/chat/sessions/{chat_id}/merge")
def merge(chat_id: int, session: Session = SessionDep) -> dict[str, Any]:
    return {"merge_sha": chat_service.merge(session, chat_service.get(session, chat_id))}


@router.delete("/chat/sessions/{chat_id}", status_code=204)
def delete_session(chat_id: int, session: Session = SessionDep) -> None:
    chat_service.delete(session, chat_service.get(session, chat_id))
