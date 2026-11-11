"""Providers, model profiles, harness adapters. Secrets are never returned: only env var
names and whether they are set."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from sqlmodel import Session, select

from orchestrator.api.dependencies import AUTH, SessionDep
from orchestrator.core import context_windows
from orchestrator.core.models import HarnessAdapter, ModelProfile, Provider
from orchestrator.core.schemas import (
    AdapterUpdate,
    ModelProfileCreate,
    ModelProfileUpdate,
    ProviderCreate,
    ProviderUpdate,
)
from orchestrator.providers.registry import build_provider
from orchestrator.services import adapter_service, provider_service

router = APIRouter(dependencies=AUTH)


def dump(obj: Any) -> dict[str, Any]:
    return json.loads(obj.model_dump_json())


@router.get("/providers")
def list_providers(session: Session = SessionDep) -> list[dict[str, Any]]:
    return [provider_service.provider_status(p) for p in session.exec(select(Provider))]


@router.post("/providers", status_code=201)
def create_provider(body: ProviderCreate, session: Session = SessionDep) -> dict[str, Any]:
    if session.exec(select(Provider).where(Provider.name == body.name)).first():
        raise HTTPException(409, "provider exists")
    p = Provider(**body.model_dump(mode="json"))
    session.add(p)
    session.commit()
    session.refresh(p)
    return provider_service.provider_status(p)


@router.patch("/providers/{provider_id}")
def update_provider(
    provider_id: int, body: ProviderUpdate, session: Session = SessionDep
) -> dict[str, Any]:
    p = session.get(Provider, provider_id)
    if not p:
        raise HTTPException(404, "provider not found")
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(p, k, v)
    session.add(p)
    session.commit()
    return provider_service.provider_status(p)


@router.post("/providers/{provider_id}/test")
async def test_provider(provider_id: int, session: Session = SessionDep) -> dict[str, Any]:
    """Configuration check only (no network call, no tokens spent)."""
    p = session.get(Provider, provider_id)
    if not p:
        raise HTTPException(404, "provider not found")
    return await build_provider(provider_service.provider_config(p)).health()


@router.get("/model-profiles")
def list_profiles(session: Session = SessionDep) -> list[dict[str, Any]]:
    return [_profile(session, m) for m in session.exec(select(ModelProfile))]


@router.post("/model-profiles", status_code=201)
def create_profile(body: ModelProfileCreate, session: Session = SessionDep) -> dict[str, Any]:
    if not session.get(Provider, body.provider_id):
        raise HTTPException(404, "provider not found")
    if session.exec(select(ModelProfile).where(ModelProfile.name == body.name)).first():
        raise HTTPException(409, f"a model profile named {body.name!r} already exists")
    m = ModelProfile(**body.model_dump(mode="json"))
    session.add(m)
    session.commit()
    session.refresh(m)
    return _profile(session, m)


@router.patch("/model-profiles/{profile_id}")
def update_profile(
    profile_id: int, body: ModelProfileUpdate, session: Session = SessionDep
) -> dict[str, Any]:
    m = session.get(ModelProfile, profile_id)
    if not m:
        raise HTTPException(404, "profile not found")
    for k, v in body.model_dump(exclude_unset=True, mode="json").items():
        setattr(m, k, v)
    session.add(m)
    session.commit()
    return _profile(session, m)


def _profile(session: Session, m: ModelProfile) -> dict[str, Any]:
    """`builtin_context_window` is what applies when the profile leaves `context_window` empty.
    `chat_harnesses` are the harnesses whose Chat model menu offers this profile's model."""
    routes = adapter_service.harness_routes(session)
    return {
        **dump(m),
        "builtin_context_window": context_windows.builtin(m.model_name),
        "chat_harnesses": adapter_service.route_for(routes, m.name, m.model_name),
    }


@router.get("/adapters")
def list_adapters(session: Session = SessionDep) -> list[dict[str, Any]]:
    return [adapter_service.describe(a) for a in session.exec(select(HarnessAdapter))]


@router.post("/adapters/detect")
def detect_adapters(session: Session = SessionDep) -> list[dict[str, Any]]:
    adapter_service.seed_defaults(session)
    return [adapter_service.describe(a) for a in session.exec(select(HarnessAdapter))]


@router.get("/adapters/{name}/health")
async def adapter_health(name: str, session: Session = SessionDep) -> dict[str, Any]:
    return await adapter_service.get_adapter(session, name).health()


@router.patch("/adapters/{name}")
def update_adapter(name: str, body: AdapterUpdate, session: Session = SessionDep) -> dict[str, Any]:
    row = adapter_service.get_row(session, name)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(row, k, v)
    session.add(row)
    session.commit()
    return adapter_service.describe(row)
