"""Opt-in background execution and runtime status for Supabase synchronization."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from sqlmodel import Session

from orchestrator.core.config import get_settings
from orchestrator.core.db import session_scope
from orchestrator.core.logging import get_logger
from orchestrator.core.models import utcnow
from orchestrator.services import settings_service
from orchestrator.services.sync_transport import SupabaseSyncClient

log = get_logger(__name__)


class SyncBusyError(RuntimeError):
    """Raised when a manual or automatic synchronization is already running."""


class SyncRunner:
    def __init__(
        self,
        transport_factory: Callable[[], SupabaseSyncClient] = SupabaseSyncClient,
        poll_seconds: float = 2.0,
    ) -> None:
        self.transport_factory = transport_factory
        self.poll_seconds = poll_seconds
        self._lock = asyncio.Lock()
        self._running = False
        self._next_run = 0.0
        self._last_started_at: str | None = None
        self._last_finished_at: str | None = None
        self._last_success_at: str | None = None
        self._last_error: str | None = None
        self._last_result: dict[str, Any] | None = None

    @staticmethod
    def configured() -> bool:
        s = get_settings()
        return all(
            (
                s.supabase_url,
                s.supabase_publishable_key,
                s.supabase_email,
                s.supabase_password,
            )
        )

    def status(self, session: Session) -> dict[str, Any]:
        return {
            "configured": self.configured(),
            "enabled": settings_service.get(session, "supabase_sync_enabled"),
            "running": self._running,
            "interval_seconds": settings_service.get(
                session, "supabase_sync_interval_seconds"
            ),
            "last_started_at": self._last_started_at,
            "last_finished_at": self._last_finished_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "last_result": self._last_result,
        }

    async def run_once(self) -> dict[str, Any]:
        """Run one pull/push cycle, regardless of the automatic-sync preference."""
        # Set this before the first await so concurrent callers cannot queue duplicate cycles.
        if self._running:
            raise SyncBusyError("a Supabase synchronization is already running")
        self._running = True
        self._last_started_at = utcnow().isoformat()
        async with self._lock:
            try:
                async with self.transport_factory() as client:
                    with session_scope() as session:
                        result = await client.sync_once(session)
                self._last_result = result
                self._last_success_at = utcnow().isoformat()
                self._last_error = None
                return result
            except Exception as exc:
                self._last_error = str(exc)
                raise
            finally:
                self._last_finished_at = utcnow().isoformat()
                self._running = False

    async def run_loop(self) -> None:
        """Poll device-local preferences; stay alive so changes need no restart."""
        while True:
            try:
                with session_scope() as session:
                    enabled = settings_service.get(session, "supabase_sync_enabled")
                    interval = settings_service.get(
                        session, "supabase_sync_interval_seconds"
                    )
                if not enabled or not self.configured():
                    # Run immediately after both enablement and configuration become available.
                    self._next_run = 0.0
                elif time.monotonic() >= self._next_run:
                    self._next_run = time.monotonic() + interval
                    try:
                        await self.run_once()
                    except SyncBusyError:
                        pass
                    except Exception:  # noqa: BLE001 - retain the error and retry next interval
                        log.exception("background Supabase synchronization failed")
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the background loop alive
                log.exception("background Supabase synchronization loop error")
            await asyncio.sleep(self.poll_seconds)


sync_runner = SyncRunner()
