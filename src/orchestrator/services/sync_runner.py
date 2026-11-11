"""Opt-in background execution, retry policy, and runtime status for Supabase sync (T-41).

The loop has exactly three outcomes for a failed cycle:

* a permanent client error (4xx other than 408/425/429) stops the loop immediately and records a
  stopped state — retrying a refusal forever is not a recovery strategy;
* a transient failure (5xx, timeout, network, 408/425/429) retries indefinitely with exponential
  backoff capped at ``backoff_max_seconds``. A transient failure never becomes terminal: a laptop
  that is offline for an hour is not the same event as a permanent 400, and must recover by
  itself when connectivity returns;
* a success clears the streak, the stop, and the log-dedup signature.

The delay is capped, not the attempt count. The first retry is never sooner than the configured
interval (``base = max(30 s, supabase_sync_interval_seconds)``), so the default sequence is
60, 120, 240, 480, 900, 900, ... seconds. ``backoff_max_seconds`` is raised to the interval when a
device polls less often than 900 s, so the documented cap is reachable at every interval.

The failure *alert* is held to the same two rules, because the code that removes an unbounded
log must not become the next one: an emission failure is deduplicated by exception signature
(one line plus a counter) and retried on the same capped backoff, with the reason, the schedule
and the counter in the health record so ``sync status`` and the GUI show a failed alert without
anyone reading a log.

The stopped state lives in the device-local ``sync_state`` table (key ``sync_health``). The
polling coroutine stays alive so enable/disable and interval changes still apply without a
restart; "stopped" means no further automatic attempts until a manual ``sync now`` succeeds or
``sync reinit --apply`` re-baselines the version counters.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from datetime import datetime, timedelta
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
DEFAULT_NOTIFY_AFTER_FAILURES = 3


def backoff_seconds(
    consecutive_failures: int,
    base: float = DEFAULT_BACKOFF_BASE_SECONDS,
    cap: float = DEFAULT_BACKOFF_MAX_SECONDS,
) -> float:
    """Exponential backoff: base, 2*base, 4*base ... capped; zero when healthy.

    The cap is a ceiling on the delay, never on the number of attempts. Pure and injectable so
    the retry sequence is asserted as a sequence rather than slept through in a test.
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
        notify_after_failures: int = DEFAULT_NOTIFY_AFTER_FAILURES,
    ) -> None:
        self.transport_factory = transport_factory
        self.poll_seconds = poll_seconds
        self.backoff_base_seconds = backoff_base_seconds
        self.backoff_max_seconds = backoff_max_seconds
        self.notify_after_failures = notify_after_failures
        self._lock = asyncio.Lock()
        self._running = False
        self._next_run = 0.0
        # In-memory signature of a notify failure that could not itself be recorded. Only the
        # first occurrence is logged, so an unwritable health record cannot reintroduce the
        # per-interval ERROR line the persisted dedupe removes.
        self._unrecorded_notify_error: str | None = None
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
            # Additive (T-41): a failed alert is an operator-visible fact, not a log line. The
            # Notifications list records the delivery outcome; these say the alert never left.
            "notify_failures": health["notify_failures"],
            "last_notify_error": health["last_notify_error"],
            "last_notify_failed_at": health["last_notify_failed_at"],
            "notify_backoff_seconds": health["notify_backoff_seconds"],
            "notify_next_retry_at": health["notify_next_retry_at"],
            "notify_log_occurrences": health["notify_log_occurrences"],
        }

    def _record_failure(self, exc: Exception) -> dict[str, Any]:
        """Persist the classified failure and apply the log-deduplication rule.

        Rule: the log line is keyed by the exact ``<exception type>: <message>`` signature.
        The first occurrence of a signature logs once, with a traceback. Every identical
        repeat only increments ``sync_health.log_occurrences`` (at DEBUG, normally invisible);
        a different signature logs again and restarts the count. That is what turns 6,682
        identical tracebacks into one line plus a counter.

        The counter and the signature are derived inside the compare-and-set mutation, never
        from a snapshot read in an earlier session: the desktop loop and a CLI ``sync now`` are
        separate processes, so a plain read-modify-write could lose an increment.
        """
        from orchestrator.services.sync_transport import SyncPermanentError

        kind = "permanent" if isinstance(exc, SyncPermanentError) else "transient"
        now = utcnow()
        signature = f"{type(exc).__name__}: {exc}"
        decision: dict[str, Any] = {}
        with session_scope() as session:
            # Only a permanent client error is terminal. A transient failure keeps retrying for
            # as long as it takes; the cap below is a ceiling on the delay, not on attempts.
            stopped = kind == "permanent"
            interval = float(
                settings_service.get(session, "supabase_sync_interval_seconds") or 0
            )
            base = max(float(self.backoff_base_seconds), interval)
            cap = max(float(self.backoff_max_seconds), base)

            def mutate(previous: dict[str, Any]) -> dict[str, Any]:
                failures = int(previous.get("consecutive_failures") or 0) + 1
                same = previous.get("last_logged_error") == signature
                occurrences = (int(previous.get("log_occurrences") or 0) if same else 0) + 1
                backoff = 0.0 if stopped else backoff_seconds(failures, base, cap)
                decision.update(
                    same_signature=same, occurrences=occurrences, failures=failures
                )
                return {
                    "consecutive_failures": failures,
                    "last_failure_at": now.isoformat(),
                    "last_failure_kind": kind,
                    "last_failure_error": str(exc)[:1000],
                    "stopped": stopped,
                    "stopped_reason": f"permanent client error: {exc}" if stopped else None,
                    "stopped_at": now.isoformat() if stopped else None,
                    "next_retry_at": (
                        (now + timedelta(seconds=backoff)).isoformat() if backoff else None
                    ),
                    "backoff_seconds": backoff,
                    "log_occurrences": occurrences,
                    "last_logged_error": signature,
                }

            values = sync_service.update_health(session, mutate)
        if not decision["same_signature"]:
            log.error(
                "background Supabase synchronization failed: %s "
                "(occurrence 1; identical repeats only increment sync_health.log_occurrences)",
                exc,
                exc_info=True,
            )
        else:
            log.debug(
                "background Supabase synchronization failed again (occurrence %d): %s",
                decision["occurrences"],
                exc,
            )
        return values

    def _record_notify_failure(self, exc: Exception) -> dict[str, Any]:
        """Count and deduplicate a failed alert emission; never raises into the caller.

        DoD 5 applies to this path too: the code that removes an unbounded log must not create
        one. The rule is the same one ``_record_failure`` uses, applied to the alert:

        * the log line is keyed by the exact ``<exception type>: <message>`` signature, so the
          first occurrence logs once with a traceback and identical repeats only increment
          ``sync_health.notify_log_occurrences`` (at DEBUG, normally invisible);
        * the retry delay is the same capped exponential backoff as the sync path, measured from
          this failure and persisted as ``notify_next_retry_at``, so a broken notifier is retried
          once per backoff instead of every interval forever;
        * the reason, the schedule and the counter are written to the health record, which is what
          ``SyncRunner.status()`` / ``orchestrator sync status`` / the GUI read.

        This method is called from inside an ``except`` block that is already handling the sync
        failure, so a failure to record the alert failure must not replace it: the recording
        write is guarded and a non-recordable signature is logged at most once.
        """
        signature = f"{type(exc).__name__}: {exc}"
        decision: dict[str, Any] = {}
        values: dict[str, Any] = {}
        try:
            with session_scope() as session:
                interval = float(
                    settings_service.get(session, "supabase_sync_interval_seconds") or 0
                )
                base = max(float(self.backoff_base_seconds), interval)
                cap = max(float(self.backoff_max_seconds), base)

                def mutate(previous: dict[str, Any]) -> dict[str, Any]:
                    failures = int(previous.get("notify_failures") or 0) + 1
                    same = previous.get("last_logged_notify_error") == signature
                    occurrences = (
                        int(previous.get("notify_log_occurrences") or 0) if same else 0
                    ) + 1
                    backoff = backoff_seconds(failures, base, cap)
                    now = utcnow()
                    decision.update(
                        same_signature=same, occurrences=occurrences, failures=failures
                    )
                    return {
                        "notify_failures": failures,
                        "last_notify_error": str(exc)[:500],
                        "last_notify_failed_at": now.isoformat(),
                        "notify_backoff_seconds": backoff,
                        "notify_next_retry_at": (
                            now + timedelta(seconds=backoff)
                        ).isoformat(),
                        "notify_log_occurrences": occurrences,
                        "last_logged_notify_error": signature,
                    }

                values = sync_service.update_health(session, mutate)
        except Exception as record_exc:  # noqa: BLE001 - must not mask the alert/sync failure
            self._log_unrecorded_notify_failure(record_exc)
            return {}

        backoff = float(values.get("notify_backoff_seconds") or 0.0)
        if not decision["same_signature"]:
            log.error(
                "Supabase sync notification path failed: %s (occurrence 1; identical repeats "
                "only increment sync_health.notify_log_occurrences; retrying in %.0f s)",
                exc,
                backoff,
                exc_info=True,
            )
        else:
            log.debug(
                "Supabase sync notification path failed again (occurrence %d; retrying in "
                "%.0f s): %s",
                decision["occurrences"],
                backoff,
                exc,
            )
        return values

    def _log_unrecorded_notify_failure(self, exc: Exception) -> None:
        """Log a notify failure that could not be written down, at most once per signature."""
        signature = f"{type(exc).__name__}: {exc}"
        if self._unrecorded_notify_error == signature:
            return
        self._unrecorded_notify_error = signature
        log.error(
            "Supabase sync notification failure could not be recorded in sync_health: %s",
            exc,
        )

    @staticmethod
    def _notify_due(health: dict[str, Any]) -> bool:
        """Is the stopped-alert emission due, per its persisted capped-backoff schedule?

        The schedule lives in the health record rather than in memory so a restarted desktop loop
        resumes the backoff instead of re-alerting every poll, and so the next attempt is visible
        through ``sync status``. An unparseable timestamp is treated as due: wedging the alert on
        a corrupt value is worse than one early duplicate.
        """
        at = health.get("notify_next_retry_at")
        if not at:
            return True
        try:
            return utcnow() >= datetime.fromisoformat(str(at))
        except (TypeError, ValueError):
            return True

    def _notify_if_needed(self, health: dict[str, Any]) -> bool:
        """At most two delivered notifications per failure streak.

        Each alert is a three-part claim, because two processes (the desktop loop and a CLI
        ``sync now``) can hit the same failing cycle:

        * ``claim_health_flag`` leases the emission with a per-attempt token whose window covers
          the whole delivery path, so a slow emission is not duplicated;
        * the flag that says "the operator was notified" is written by
          ``complete_health_flag`` only AFTER ``log_service.log_event`` returned, as a
          compare-and-set on that token -- a record derived from the actual result, never from
          the intent to emit;
        * a failed emission RELEASES the claim, records its error separately in the health
          record, and leaves the flag false, so the next cycle tries again instead of the streak
          being silently marked as announced.

        The notification fan-out is the T-9 trigger mechanism, so the master switch and the
        per-trigger subscription decide whether a popup happens; either way the event is visible
        in the Notifications list and the build log.
        """
        from orchestrator.services import log_service

        failures = int(health.get("consecutive_failures") or 0)
        if health.get("stopped"):
            flag = "stop_notified"
            details = (
                f"Supabase sync stopped after {failures} failure(s): "
                f"{health.get('stopped_reason') or health.get('last_failure_error') or 'unknown'}"
            )
        elif failures >= self.notify_after_failures:
            flag = "streak_notified"
            details = (
                f"Supabase sync has failed {failures} consecutive times: "
                f"{health.get('last_failure_error') or 'unknown'}"
            )
        else:
            return False

        with session_scope() as session:
            # Claim before logging: the loser of the race writes nothing and logs nothing.
            token = sync_service.claim_health_flag(session, flag)
        if token is None:
            return False

        try:
            with session_scope() as session:
                log_service.log_event(session, "sync_failed", details, role="system")
        except Exception as exc:  # noqa: BLE001 - the sync failure being reported must survive
            # An emission failure must never replace the original sync exception (the caller is
            # already in an ``except`` block) and must never leave the streak looking announced.
            try:
                with session_scope() as session:
                    sync_service.release_health_flag(
                        session, flag, token, error=f"{type(exc).__name__}: {exc}"
                    )
            finally:
                # Release first (so the streak can notify again), then record the failure under
                # the same dedupe/backoff rule as the sync path: one line per signature with an
                # occurrence count, and a capped delay before the next attempt.
                self._record_notify_failure(exc)
            return False

        with session_scope() as session:
            settled = sync_service.complete_health_flag(session, flag, token)
        if not settled:
            # The claim expired or was cleared (for example a successful cycle) while the
            # emission ran. The event itself still went out, but this runner must not report a
            # flag write it did not land.
            log.debug("Supabase sync notification claim was settled by another writer")
        return settled

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
                # Notify from run_once, not only from the loop: a manual `sync now` that stops the
                # sync must reach the operator too. The persisted flags make this idempotent, so
                # the loop calling it as well would not double-alert. ``_notify_if_needed`` handles
                # its own emission failures, but this guard is deliberate: no notification problem
                # may replace the sync exception the caller has to see.
                try:
                    self._notify_if_needed(health)
                except Exception as exc:  # noqa: BLE001 - never mask the original sync failure
                    # Same dedupe and capped backoff as an emission failure: the claim path (or
                    # the health write) failing must not become a per-interval ERROR line.
                    self._record_notify_failure(exc)
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
                    # Only a permanent refusal is terminal; keep polling preferences and make no
                    # further automatic attempts until a human repairs the state. The alert is
                    # the exception: an emission that failed released its claim, so retry it on
                    # its own capped-backoff schedule (``notify_next_retry_at``, persisted, so a
                    # restart resumes it) rather than every poll -- a broken notifier must not
                    # become an unbounded ERROR source.
                    if not health.get("stop_notified") and self._notify_due(health):
                        try:
                            self._notify_if_needed(health)
                        except Exception as exc:  # noqa: BLE001 - keep the background loop alive
                            self._record_notify_failure(exc)
                    self._next_run = 0.0
                elif time.monotonic() >= self._next_run:
                    self._next_run = time.monotonic() + interval
                    try:
                        await self.run_once()
                    except SyncBusyError:
                        pass
                    except Exception:  # noqa: BLE001 - classified, persisted, and notified
                        with session_scope() as session:
                            health = sync_service.health(session)
                        if not health["stopped"]:
                            # Back off from the failure time, not from the interval boundary.
                            self._next_run = time.monotonic() + max(
                                float(health["backoff_seconds"] or 0), self.poll_seconds
                            )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - keep the background loop alive
                log.exception("background Supabase synchronization loop error")
            await asyncio.sleep(self.poll_seconds)


sync_runner = SyncRunner()
