"""Opt-in background execution, retry policy, and runtime status for Supabase sync (T-41).

The loop has exactly three outcomes for a failed cycle:

* a permanent client error (4xx other than 408/425/429) stops the loop immediately and records a
  stopped state — retrying a refusal forever is not a recovery strategy;
* a transient failure (5xx, timeout, network, 408/429) retries with exponential backoff, capped by
  ``max_consecutive_failures``, after which the loop stops as well;
* a success clears the streak, the stop, and the log-dedup signature.

The stopped state lives in the device-local ``sync_state`` table (key ``sync_health``). The
polling coroutine stays alive so enable/disable and interval changes still apply without a
restart; "stopped" means no further automatic attempts until a manual ``sync now`` succeeds or
``sync reinit --apply`` re-baselines the version counters.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from sqlmodel import Session

from orchestrator.core.config import get_settings
from orchestrator.core.db import session_scope
from orchestrator.core.logging import get_logger
from orchestrator.core.models import utcnow
from orchestrator.services import settings_service, sync_service
from orchestrator.services.sync_transport import SupabaseSyncClient

log = get_logger(__name__)

DEFAULT_BACKOFF_BASE_SECONDS = 30.0
DEFAULT_BACKOFF_MAX_SECONDS = 900.0
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5
DEFAULT_NOTIFY_AFTER_FAILURES = 3


def backoff_seconds(
    consecutive_failures: int,
    base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    cap: float = DEFAULT_BACKOFF_MAX_SECONDS,
) -> float:
    """Exponential backoff: base, 2*base, 4*base ... capped; zero when healthy.

    Pure and injectable so the retry sequence is asserted as a sequence rather than slept
    through in a test.
    """
    if consecutive_failures <= 0:
        return 0.0
    return min(float(base) * (2 ** (consecutive_failures - 1)), float(cap))


class SyncBusyError(RuntimeError):
    """Raised when a manual or automatic synchronization is already running."""


class SyncRunner:
    def __init__(
        self,
        transport_factory: Callable[[], SupabaseSyncClient] = SupabaseSyncClient,
        poll_seconds: float = 2.0,
        *,
        backoff_base_seconds: float = DEFAULT_BACKOFF_BASE_SECONDS,
        backoff_max_seconds: float = DEFAULT_BACKOFF_MAX_SECONDS,
        max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
        notify_after_failures: int = DEFAULT_NOTIFY_AFTER_FAILURES,
    ) -> None:
        self.transport_factory = transport_factory
        self.poll_seconds = poll_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.max_consecutive_failures = max_consecutive_failures
        self.notify_after_failures = notify_after_failures
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
        health = sync_service.health(session)
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
            # Additive health keys (T-41): the same record the loop uses, so a restart cannot
            # hide a stopped sync from `sync status` or the GUI.
            "consecutive_failures": health["consecutive_failures"],
            "last_failure_at": health["last_failure_at"],
            "last_failure_kind": health["last_failure_kind"],
            "last_failure_error": health["last_failure_error"],
            "stopped": health["stopped"],
            "stopped_reason": health["stopped_reason"],
            "next_retry_at": health["next_retry_at"],
            "backoff_seconds": health["backoff_seconds"],
            "log_occurrences": health["log_occurrences"],
        }

    def _record_failure(self, exc: Exception) -> dict[str, Any]:
        """Persist the classified failure and apply the log-deduplication rule.

        Rule: the log line is keyed by the exact ``<exception type>: <message>`` signature.
        The first occurrence of a signature logs once, with a traceback. Every identical
        repeat only increments ``sync_health.log_occurrences`` (at DEBUG, normally invisible);
        a different signature logs again and restarts the count. That is what turns 6,682
        identical tracebacks into one line plus a counter.
        """
        from orchestrator.services.sync_transport import SyncPermanentError

        kind = "permanent" if isinstance(exc, SyncPermanentError) else "transient"
        now = utcnow()
        with session_scope() as session:
            previous = sync_service.health(session)
            signature = f"{type(exc).__name__}: {exc}"
            same_signature = previous.get("last_logged_error") == signature
            occurrences = int(previous.get("log_occurrences") or 0) + 1 if same_signature else 1
            failures = int(previous.get("consecutive_failures") or 0) + 1
            stopped = kind == "permanent" or failures >= self.max_consecutive_failures
            backoff = 0.0 if stopped else backoff_seconds(
                failures, self.backoff_base_seconds, self.backoff_max_seconds
            )
            if kind == "permanent":
                reason = f"permanent client error: {exc}"
            elif stopped:
                reason = (
                    f"{failures} consecutive transient failures; last: {exc}"
                )
            else:
                reason = None
            values = sync_service.write_health(
                session,
                consecutive_failures=failures,
                last_failure_at=now.isoformat(),
                last_failure_kind=kind,
                last_failure_error=str(exc)[:1000],
                stopped=stopped,
                stopped_reason=reason if stopped else None,
                stopped_at=now.isoformat() if stopped else None,
                next_retry_at=(now + timedelta(seconds=backoff)).isoformat() if backoff else None,
                backoff_seconds=backoff,
                log_occurrences=occurrences,
                last_logged_error=signature,
            )
        if not same_signature:
            log.error(
                "background Supabase synchronization failed: %s "
                "(occurrence 1; identical repeats only increment sync_health.log_occurrences)",
                exc,
                exc_info=True,
            )
        else:
            log.debug(
                "background Supabase synchronization failed again (occurrence %d): %s",
                occurrences,
                exc,
            )
        return values

    def _notify_if_needed(self, health: dict[str, Any]) -> bool:
        """At most two build-log events (and so notifications) per failure streak.

        One when a transient streak becomes persistent (``notify_after_failures``) and one when
        the loop stops. Each is guarded by a persisted flag, so a restart cannot replay the same
        alert every interval. The notification fan-out is the T-9 trigger mechanism, so the
        master switch and the per-trigger subscription decide whether a popup happens; either
        way the event is visible in the Notifications list and the build log.
        """
        from orchestrator.services import log_service

        failures = int(health.get("consecutive_failures") or 0)
        if health.get("stopped"):
            if health.get("stop_notified"):
                return False
            details = (
                f"Supabase sync stopped after {failures} failure(s): "
                f"{health.get('stopped_reason') or health.get('last_failure_error') or 'unknown'}"
            )
            with session_scope() as session:
                log_service.log_event(session, "sync_failed", details, role="system")
                sync_service.write_health(
                    session, streak_notified=True, stop_notified=True
                )
            return True
        if health.get("streak_notified") or failures < self.notify_after_failures:
            return False
        details = (
            f"Supabase sync has failed {failures} consecutive times: "
            f"{health.get('last_failure_error') or 'unknown'}"
        )
        with session_scope() as session:
            log_service.log_event(session, "sync_failed", details, role="system")
            sync_service.write_health(session, streak_notified=True)
        return True

    async def run_once(self) -> dict[str, Any]:
        """Run one pull/push cycle, regardless of the automatic-sync preference.

        A manual run is intentionally allowed while the loop is stopped: it is the operator's
        explicit retry after a repair, and a success clears the stopped state.
        """
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
                with session_scope() as session:
                    sync_service.clear_health(session)
                self._last_result = result
                self._last_success_at = utcnow().isoformat()
                self._last_error = None
                return result
            except SyncBusyError:
                raise
            except Exception as exc:
                self._last_error = str(exc)
                health = self._record_failure(exc)
                self._next_run = time.monotonic() + float(health["backoff_seconds"] or 0)
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
                    health = sync_service.health(session)
                if not enabled or not self.configured():
                    # Run immediately after both enablement and configuration become available.
                    self._next_run = 0.0
                elif health["stopped"]:
                    # A permanent refusal (or a capped transient streak) is terminal until a
                    # human fixes the state. Keep polling preferences, attempt nothing.
                    self._next_run = 0.0
                elif time.monotonic() >= self._next_run:
                    self._next_run = time.monotonic() + interval
                    try:
                        await self.run_once()
                    except SyncBusyError:
                        pass
                    except Exception:  # noqa: BLE001 - classified and persisted by run_once
                        with session_scope() as session:
                            health = sync_service.health(session)
                        if not health["stopped"]:
                            # Back off from the failure time, not from the interval boundary.
                            self._next_run = time.monotonic() + max(
                                float(health["backoff_seconds"] or 0), self.poll_seconds
                            )
                        self._notify_if_needed(health)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the background loop alive
                log.exception("background Supabase synchronization loop error")
            await asyncio.sleep(self.poll_seconds)


sync_runner = SyncRunner()
