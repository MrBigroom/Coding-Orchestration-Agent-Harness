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
    """A transport call failed. Subclasses carry the retry classification."""


class SyncPermanentError(SyncTransportError):
    """A client error that retrying cannot fix: stop the loop until a human intervenes.

    ``detail`` is the machine-readable half of the frozen ``{error, detail}`` API shape; the
    exception message is only the human sentence. Callers must never need to substring-match
    the sentence to learn the status code or the recovery path (T-41).
    """

    def __init__(self, message: str, *, status_code: int | None = None,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"kind": "permanent", "status_code": status_code, **(detail or {})}


class SyncTransientError(SyncTransportError):
    """A failure worth retrying with backoff: 5xx, 408/429, timeout, or network error."""

    def __init__(self, message: str, *, status_code: int | None = None,
                 detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.detail = {"kind": "transient", "status_code": status_code, **(detail or {})}


# 408 (request timeout) and 429 (rate limited) are client status codes that are still
# transient; every other 4xx is a permanent refusal until the configuration or state changes.
TRANSIENT_CLIENT_STATUSES = frozenset({408, 425, 429})

# The structured ``recovery`` text is derived from the error CLASS, never from where the failure
# was raised. Credentials advice on a 5xx or a timeout names the wrong repair path: the operator
# would go looking for a bad password while the loop is already retrying by itself. Each side of
# the classification therefore states its own next step, including what would stop a retry.
RECOVERY_BY_KIND: dict[str, str] = {
    "permanent": (
        "check ORCHESTRATOR_SUPABASE_EMAIL and ORCHESTRATOR_SUPABASE_PASSWORD in .env, "
        "then run `orchestrator sync now`"
    ),
    "transient": (
        "the sync loop retries this with exponential backoff; only a permanent 4xx response "
        "stops it and notifies the operator"
    ),
}


def recovery_for(kind: str) -> str:
    """The repair path for a classified transport failure (see ``RECOVERY_BY_KIND``)."""
    return RECOVERY_BY_KIND[kind]



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

    @staticmethod
    def _response_error(response: httpx.Response) -> SyncTransportError:
        """Classify one non-2xx response; the frozen API can return ``detail`` structurally."""
        message = SupabaseSyncClient._safe_error(response)
        detail = {
            "status_code": response.status_code,
            "body": message,
            "retryable": False,
        }
        if response.status_code >= 500 or response.status_code in TRANSIENT_CLIENT_STATUSES:
            detail["retryable"] = True
            # A transient status is unambiguous, so the repair path comes from the class here and
            # every caller of this classifier (authentication or request) carries it.
            detail["recovery"] = recovery_for("transient")
            return SyncTransientError(message, status_code=response.status_code, detail=detail)
        if "invalid version transition" in message:
            # A 4xx version rejection is the legacy local/remote counter drift. It is still
            # permanent for this process, but the detail names the dry-run recovery command so
            # the GUI/CLI never has to parse the sentence.
            detail["recovery"] = "orchestrator sync reinit --dry-run"
        return SyncPermanentError(message, status_code=response.status_code, detail=detail)

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
            # A timeout/connection failure is a transient class with its own repair path: say that
            # it is retried with backoff and what would stop it. Leaving ``recovery`` off this
            # branch sent a timeout out with no structured next step while a 503 carried one.
            raise SyncTransientError(
                f"Supabase authentication failed: {exc}",
                detail={
                    "retryable": True,
                    "cause": type(exc).__name__,
                    "recovery": recovery_for("transient"),
                },
            ) from exc
        if response.status_code >= 400:
            error = self._response_error(response)
            error.detail["stage"] = "authenticate"
            # Only a genuine credential refusal gets credential guidance. ``setdefault`` keeps a
            # more specific recovery already classified for this response (a version rejection
            # names the reinit command); otherwise the class decides the sentence.
            error.detail.setdefault("recovery", recovery_for(error.detail["kind"]))
            raise error
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
            # Same class-derived recovery as the auth path: a request-level network failure is
            # transient and must name the retry behaviour, never the credential repair.
            raise SyncTransientError(
                f"Supabase request failed: {exc}",
                detail={
                    "retryable": True,
                    "cause": type(exc).__name__,
                    "recovery": recovery_for("transient"),
                },
            ) from exc
        if response.status_code >= 400:
            raise self._response_error(response)
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
                raise SyncPermanentError(
                    "Supabase pull response must be a JSON array",
                    detail={"retryable": False, "response_type": type(records).__name__},
                )
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
            next_version = mapping.synced_version + 1
            response = await self._request(
                "POST",
                "/rest/v1/rpc/orchestrator_sync_push",
                json={
                    "p_entity_type": mapping.entity_type,
                    "p_entity_id": mapping.uuid,
                    "p_expected_version": mapping.synced_version,
                    "p_version": next_version,
                    "p_payload": payload,
                    "p_origin_device": mapping.origin_device,
                    "p_device_modified_at": mapping.modified_at.isoformat(),
                },
            )
            body = response.json()
            if body.get("status") == "ok":
                # The remote version is always the last synchronized version plus one. It is
                # NOT the local revision counter (`mapping.version`): that one can jump by more
                # than one when a record changes twice before a push, which is exactly how the
                # `invalid version transition: 0 -> 3` failure was produced. Record the version
                # that actually landed so the next push starts from it.
                mapping.version = next_version
                sync_service.mark_synced(session, mapping)
                result["pushed"] += 1
            elif body.get("status") == "conflict":
                mapping.conflict_json = body.get("record")
                mapping.dirty = True
                session.add(mapping)
                session.commit()
                result["conflicts"] += 1
            else:
                raise SyncPermanentError(
                    "Supabase push response had an unknown status",
                    detail={"retryable": False, "status": body.get("status")},
                )
        return result

    async def remote_versions(self) -> dict[str, int]:
        """Read the remote entity versions without applying them (dry-run re-initialisation).

        The same pull shape is used, but only ``entity_type``/``entity_id``/``version`` are
        selected so a dry run can plan against remote truth without writing local rows.
        """
        versions: dict[str, int] = {}
        limit = self.settings.supabase_sync_batch_size
        offset = 0
        while True:
            response = await self._request(
                "GET",
                "/rest/v1/orchestrator_sync_records",
                params={
                    "select": "entity_type,entity_id,version",
                    "order": "entity_type.asc,entity_id.asc",
                    "limit": limit,
                    "offset": offset,
                },
            )
            records = response.json()
            if not isinstance(records, list):
                raise SyncPermanentError(
                    "Supabase version response must be a JSON array",
                    detail={"retryable": False, "response_type": type(records).__name__},
                )
            for record in records:
                entity_id = record.get("entity_id")
                version = record.get("version")
                if not isinstance(entity_id, str) or not isinstance(version, int):
                    raise SyncPermanentError(
                        "Supabase version row is missing entity_id/version",
                        detail={"retryable": False, "record": record},
                    )
                versions[entity_id] = version
            if len(records) < limit:
                break
            offset += len(records)
        return versions

    async def sync_once(self, session: Session) -> dict[str, Any]:
        # Pull first so a fresh device can adopt matching seeded rows before uploading.
        sync_service.scan_local(session)
        pulled = await self.pull(session)
        pushed = await self.push(session)
        return {"pulled": pulled, "pushed": pushed, "status": sync_service.status(session)}
