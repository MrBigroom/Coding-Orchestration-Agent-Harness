"""Saved agents."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter
from sqlmodel import Session, col, select

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.core.models import Task
from orchestrator.core.schemas import AgentCreate, AgentRun, AgentUpdate
from orchestrator.services import agent_service, standalone_service

router = APIRouter(dependencies=AUTH)


def dump(obj: Any) -> dict[str, Any]:
    return json.loads(obj.model_dump_json())


@router.get("/agents")
def list_agents(session: Session = SessionDep) -> list[dict[str, Any]]:
    return [dump(a) for a in agent_service.list_agents(session)]


@router.post("/agents", status_code=201)
def create_agent(body: AgentCreate, session: Session = SessionDep) -> dict[str, Any]:
    return dump(agent_service.create(session, body))


@router.get("/agents/{agent_id}")
def get_agent(agent_id: int, session: Session = SessionDep) -> dict[str, Any]:
    agent = agent_service.get(session, agent_id)
    tasks = session.exec(
        select(Task).where(Task.agent_id == agent.id).order_by(col(Task.id).desc()).limit(20)
    )
    return {
        **dump(agent),
        "routing": agent_service.routing(session, agent),
        "recent_tasks": [dump(t) for t in tasks],
    }


@router.patch("/agents/{agent_id}")
def update_agent(agent_id: int, body: AgentUpdate, session: Session = SessionDep) -> dict[str, Any]:
    return dump(agent_service.update(session, agent_service.get(session, agent_id), body))


@router.delete("/agents/{agent_id}", status_code=204)
def delete_agent(agent_id: int, session: Session = SessionDep) -> None:
    agent_service.delete(session, agent_service.get(session, agent_id))


@router.get("/agents/{agent_id}/outputs")
def agent_outputs(agent_id: int, session: Session = SessionDep) -> dict[str, Any]:
    agent = agent_service.get(session, agent_id)
    runs = standalone_service.list_runs(session, agent)
    return {"folder": str(standalone_service.output_folder(agent)), "runs": [dump(r) for r in runs]}


@router.get("/standalone-runs/{run_id}/report")
def standalone_report(run_id: int, session: Session = SessionDep) -> dict[str, Any]:
    return {"text": standalone_service.read_report(standalone_service.get(session, run_id))}


@router.post("/standalone-runs/{run_id}/stop")
async def stop_standalone(run_id: int, session: Session = SessionDep) -> dict[str, Any]:
    await standalone_service.stop(standalone_service.get(session, run_id))
    session.expire_all()
    return dump(standalone_service.get(session, run_id))


@router.post("/agents/{agent_id}/run", status_code=201)
def run_agent(agent_id: int, body: AgentRun, session: Session = SessionDep) -> dict[str, Any]:
    return dump(agent_service.run(session, agent_service.get(session, agent_id), body))
