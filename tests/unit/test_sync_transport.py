import asyncio
import json

import httpx
import pytest

from orchestrator.core.config import Settings
from orchestrator.core.db import session_scope
from orchestrator.core.schemas import ProjectCreate
from orchestrator.services import project_service, sync_service
from orchestrator.services.sync_transport import SupabaseSyncClient, SyncConfigurationError


def _settings():
    return Settings(
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="publishable-test-key",
        supabase_email="user@example.test",
        supabase_password="local-test-password",
        supabase_sync_batch_size=100,
    )


def _auth_response(request):
    if request.url.path == "/auth/v1/token":
        return httpx.Response(
            200,
            json={
                "access_token": "access-test-token",
                "refresh_token": "refresh-test-token",
                "expires_in": 3600,
            },
        )
    return None


def test_sync_once_authenticates_pulls_then_pushes(initialized, tmp_path):
    (tmp_path / "project").mkdir()
    requests = []
    rpc_bodies = []

    def handler(request):
        requests.append(request)
        auth = _auth_response(request)
        if auth is not None:
            return auth
        if request.url.path == "/rest/v1/orchestrator_sync_records":
            assert request.headers["Authorization"] == "Bearer access-test-token"
            return httpx.Response(200, json=[])
        if request.url.path == "/rest/v1/rpc/orchestrator_sync_push":
            body = json.loads(request.content)
            rpc_bodies.append(body)
            return httpx.Response(200, json={"status": "ok", "record": {}})
        return httpx.Response(404)

    with session_scope() as session:
        project = project_service.create(
            session, ProjectCreate(name="Cloud", path=str(tmp_path / "project"))
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = SupabaseSyncClient(_settings(), http)
        result = asyncio.run(client.sync_once(session))
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        assert result["pushed"]["pushed"] == len(rpc_bodies)
        assert result["pushed"]["pushed"] > 0
        assert mapping.dirty is False and mapping.synced_version == 1
        assert requests[0].url.path == "/auth/v1/token"
        assert requests[1].url.path == "/rest/v1/orchestrator_sync_records"
        assert all(body["p_expected_version"] == 0 for body in rpc_bodies)
        project_body = next(body for body in rpc_bodies if body["p_entity_type"] == "project")
        assert "path" not in project_body["p_payload"]
        assert "local-test-password" not in json.dumps(rpc_bodies)
        asyncio.run(http.aclose())


def test_push_conflict_is_retained_for_resolution(initialized, tmp_path):
    (tmp_path / "project").mkdir()

    def handler(request):
        auth = _auth_response(request)
        if auth is not None:
            return auth
        body = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "status": "conflict",
                "record": {
                    "entity_type": body["p_entity_type"],
                    "entity_id": body["p_entity_id"],
                    "version": body["p_version"] + 1,
                    "payload": {},
                },
            },
        )

    with session_scope() as session:
        project = project_service.create(
            session, ProjectCreate(name="Cloud", path=str(tmp_path / "project"))
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = SupabaseSyncClient(_settings(), http)
        result = asyncio.run(client.push(session))
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        assert result["conflicts"] > 0
        assert mapping.conflict_json is not None and mapping.dirty is True
        assert not sync_service.pending(session)
        asyncio.run(http.aclose())


def test_missing_configuration_fails_without_network(initialized):
    client = SupabaseSyncClient(
        Settings(
            supabase_url=None,
            supabase_publishable_key=None,
            supabase_email=None,
            supabase_password=None,
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500))),
    )
    with session_scope() as session, pytest.raises(SyncConfigurationError):
        asyncio.run(client.pull(session))
    asyncio.run(client.http.aclose())
