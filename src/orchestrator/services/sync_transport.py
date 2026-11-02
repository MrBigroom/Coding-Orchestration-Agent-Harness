"""Authenticated Supabase transport for initial upload/download synchronization."""

from __future__ import annotations

import time
from typing import Any

import httpx
from sqlmodel import Session

from orchestrator.core.config import Settings, get_settings
from orchestrator.services import sync_service


class SyncError(ValueError):
    pass


class SyncConfigurationError(SyncError):
    pass


class SyncTransportError(SyncError):
    pass


class SupabaseSyncClient:
    def __init__(
        self,
        settings: Settings | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.http = http or httpx.AsyncClient(timeout=30)
        self._owns_http = http is None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at = 0.0

    async def __aenter__(self) -> SupabaseSyncClient:
        return self

    async def __aexit__(self, *_args: Any) -> None:
        if self._owns_http:
            await self.http.aclose()

    def _configuration(self) -> tuple[str, str, str, str]:
        values = (
            self.settings.supabase_url,
            self.settings.supabase_publishable_key,
            self.settings.supabase_email,
            self.settings.supabase_password,
        )
        if not all(values):
            raise SyncConfigurationError(
                "set ORCHESTRATOR_SUPABASE_URL, ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY, "
                "ORCHESTRATOR_SUPABASE_EMAIL, and ORCHESTRATOR_SUPABASE_PASSWORD"
            )
        return tuple(str(value) for value in values)  # type: ignore[return-value]

    @staticmethod
    def _safe_error(response: httpx.Response) -> str:
        try:
            body = response.json()
            message = body.get("msg") or body.get("message") or body.get("error") or body
        except ValueError:
            message = response.text
        return f"Supabase returned HTTP {response.status_code}: {str(message)[:500]}"

    async def _authenticate(self, refresh: bool = False) -> None:
        url, key, email, password = self._configuration()
        if refresh and self._refresh_token:
            endpoint = "/auth/v1/token?grant_type=refresh_token"
            body = {"refresh_token": self._refresh_token}
        else:
            endpoint = "/auth/v1/token?grant_type=password"
            body = {"email": email, "password": password}
        try:
            response = await self.http.post(
                f"{url.rstrip('/')}{endpoint}", headers={"apikey": key}, json=body
            )
        except httpx.HTTPError as exc:
            raise SyncTransportError(f"Supabase authentication failed: {exc}") from exc
        if response.status_code >= 400:
            raise SyncTransportError(self._safe_error(response))
        data = response.json()
        self._access_token = data["access_token"]
        self._refresh_token = data.get("refresh_token") or self._refresh_token
        self._expires_at = time.monotonic() + max(1, int(data.get("expires_in", 3600)) - 60)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        url, key, _email, _password = self._configuration()
        if not self._access_token or time.monotonic() >= self._expires_at:
            await self._authenticate(refresh=bool(self._refresh_token))
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {self._access_token}",
            **kwargs.pop("headers", {}),
        }
        try:
            response = await self.http.request(
                method, f"{url.rstrip('/')}{path}", headers=headers, **kwargs
            )
            if response.status_code == 401 and self._refresh_token:
                await self._authenticate(refresh=True)
                headers["Authorization"] = f"Bearer {self._access_token}"
                response = await self.http.request(
                    method, f"{url.rstrip('/')}{path}", headers=headers, **kwargs
                )
        except httpx.HTTPError as exc:
            raise SyncTransportError(f"Supabase request failed: {exc}") from exc
        if response.status_code >= 400:
            raise SyncTransportError(self._safe_error(response))
        return response

    async def pull(self, session: Session) -> dict[str, int]:
        totals = {"applied": 0, "adopted": 0, "skipped": 0, "conflicts": 0, "errors": 0}
        limit = self.settings.supabase_sync_batch_size
        offset = 0
        while True:
            response = await self._request(
                "GET",
                "/rest/v1/orchestrator_sync_records",
                params={
                    "select": (
                        "entity_type,entity_id,version,payload,origin_device,"
                        "device_modified_at,server_updated_at"
                    ),
                    "order": "server_updated_at.asc,entity_type.asc,entity_id.asc",
                    "limit": limit,
                    "offset": offset,
                },
            )
            records = response.json()
            if not isinstance(records, list):
                raise SyncTransportError("Supabase pull response must be a JSON array")
            result = sync_service.apply_remote_records(session, records)
            for key in totals:
                totals[key] += result[key]
            if len(records) < limit:
                break
            offset += len(records)
        return totals

    async def push(self, session: Session) -> dict[str, int]:
        sync_service.scan_local(session)
        result = {"pushed": 0, "conflicts": 0, "errors": 0}
        for mapping in sync_service.pending(session):
            spec = sync_service.SPEC_BY_TYPE[mapping.entity_type]
            row = sync_service.local_row(session, spec, mapping.local_id)
            if row is None:
                # Deletion tombstones are a later, dependency-safe synchronization phase.
                result["errors"] += 1
                continue
            payload = sync_service.canonical_payload(session, spec, row)
            response = await self._request(
                "POST",
                "/rest/v1/rpc/orchestrator_sync_push",
                json={
                    "p_entity_type": mapping.entity_type,
                    "p_entity_id": mapping.uuid,
                    "p_expected_version": mapping.synced_version,
                    "p_version": mapping.version,
                    "p_payload": payload,
                    "p_origin_device": mapping.origin_device,
                    "p_device_modified_at": mapping.modified_at.isoformat(),
                },
            )
            body = response.json()
            if body.get("status") == "ok":
                sync_service.mark_synced(session, mapping)
                result["pushed"] += 1
            elif body.get("status") == "conflict":
                mapping.conflict_json = body.get("record")
                mapping.dirty = True
                session.add(mapping)
                session.commit()
                result["conflicts"] += 1
            else:
                raise SyncTransportError("Supabase push response had an unknown status")
        return result

    async def sync_once(self, session: Session) -> dict[str, Any]:
        # Pull first so a fresh device can adopt matching seeded rows before uploading.
        sync_service.scan_local(session)
        pulled = await self.pull(session)
        pushed = await self.push(session)
        return {"pulled": pulled, "pushed": pushed, "status": sync_service.status(session)}
