"""T-41: version protocol, permanent/transient classification, backoff, dedupe, and reinit.

Every test is offline: the transport is a fake client or an httpx.MockTransport. No test opens a
network connection or talks to a real Supabase project.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from contextlib import suppress

import httpx
import pytest
from sqlmodel import select

from orchestrator.core.config import Settings, reset_settings_cache
from orchestrator.core.db import session_scope
from orchestrator.core.models import Notification
from orchestrator.core.schemas import ProjectCreate
from orchestrator.services import project_service, settings_service, sync_service
from orchestrator.services.sync_runner import SyncRunner, backoff_seconds
from orchestrator.services.sync_transport import (
    VERSION_REJECTION_RECOVERY,
    SupabaseSyncClient,
    SyncPermanentError,
    SyncTransientError,
    recovery_for,
)

LOGGER = "orchestrator.services.sync_runner"


@pytest.fixture(autouse=True)
def _no_notification_timer(monkeypatch):
    from orchestrator.services import notification_service

    monkeypatch.setattr(notification_service, "COALESCE_SECONDS", 0.0)
    monkeypatch.setattr(notification_service, "_timer", None)


def _settings() -> Settings:
    return Settings(
        supabase_url="https://example.supabase.co",
        supabase_publishable_key="publishable-test-key",
        supabase_email="user@example.test",
        supabase_password="local-test-password",
        supabase_sync_batch_size=100,
    )


def _configured(monkeypatch) -> None:
    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY", "publishable-test-key")
    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_EMAIL", "user@example.test")
    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_PASSWORD", "local-test-password")
    reset_settings_cache()


def _enable_sync() -> None:
    with session_scope() as session:
        settings_service.update(session, {"supabase_sync_enabled": True})


class FailingClient:
    def __init__(self, error, calls, reached=None):
        self.error = error
        self.calls = calls
        self.reached = reached

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def sync_once(self, _session):
        self.calls.append(type(self.error).__name__)
        if self.reached is not None:
            self.reached.set()
        raise self.error


class SuccessClient:
    def __init__(self, calls, reached=None):
        self.calls = calls
        self.reached = reached

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def sync_once(self, _session):
        self.calls.append("sync")
        if self.reached is not None:
            self.reached.set()
        return {"pulled": {}, "pushed": {}, "status": {}}


async def _run_until(reached: asyncio.Event, runner: SyncRunner) -> None:
    task = asyncio.create_task(runner.run_loop())
    await asyncio.wait_for(reached.wait(), timeout=2)
    # Give the loop several more polls: a stopped loop must make no further attempts.
    await asyncio.sleep(runner.poll_seconds * 6)
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def test_response_status_classification_separates_permanent_from_transient():
    """The 400 that stopped cloud sync is permanent; 5xx/408/429 are retryable."""
    def response(status_code: int, message: str) -> httpx.Response:
        return httpx.Response(status_code, json={"message": message})

    permanent = SupabaseSyncClient._response_error(
        response(400, "invalid version transition: 0 -> 3")
    )
    assert isinstance(permanent, SyncPermanentError)
    assert permanent.detail["kind"] == "permanent"
    assert permanent.detail["recovery"] == VERSION_REJECTION_RECOVERY
    assert "orchestrator sync reinit" in permanent.detail["recovery"]

    for status_code in (500, 503, 408, 429):
        transient = SupabaseSyncClient._response_error(response(status_code, "unavailable"))
        assert isinstance(transient, SyncTransientError)
        assert transient.detail["kind"] == "transient"
        assert transient.detail["retryable"] is True
        assert transient.detail["recovery"] == recovery_for("transient")


def test_authentication_recovery_text_is_derived_from_the_error_class():
    """A 5xx/timeout must not send the operator looking for a bad password."""
    def client_for(status_code: int, message: str) -> tuple[SupabaseSyncClient, httpx.AsyncClient]:
        def handler(_request):
            return httpx.Response(status_code, json={"message": message})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return SupabaseSyncClient(_settings(), http), http

    def failing_client_for(
        error: Exception,
    ) -> tuple[SupabaseSyncClient, httpx.AsyncClient]:
        def handler(_request):
            raise error

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        return SupabaseSyncClient(_settings(), http), http

    permanent_client, permanent_http = client_for(401, "invalid credentials")
    with pytest.raises(SyncPermanentError) as permanent:
        asyncio.run(permanent_client._authenticate())
    asyncio.run(permanent_http.aclose())

    transient_client, transient_http = client_for(503, "service unavailable")
    with pytest.raises(SyncTransientError) as transient:
        asyncio.run(transient_client._authenticate())
    asyncio.run(transient_http.aclose())

    # The network/timeout branch is a transient class too: it must carry the same structured
    # repair path as a 503, not an empty detail (finding R-87-2).
    for timeout_type in (httpx.ConnectTimeout, httpx.ConnectError):
        timeout_client, timeout_http = failing_client_for(timeout_type("connection timed out"))
        with pytest.raises(SyncTransientError) as timeout:
            asyncio.run(timeout_client._authenticate())
        asyncio.run(timeout_http.aclose())
        assert timeout.value.detail["kind"] == "transient"
        assert timeout.value.detail["cause"] == timeout_type.__name__
        assert timeout.value.detail["recovery"] == recovery_for("transient")
        assert "backoff" in timeout.value.detail["recovery"]

    permanent_recovery = permanent.value.detail["recovery"]
    transient_recovery = transient.value.detail["recovery"]
    assert permanent.value.detail["kind"] == "permanent"
    assert transient.value.detail["kind"] == "transient"
    assert permanent_recovery == recovery_for("permanent")
    assert transient_recovery == recovery_for("transient")
    assert permanent_recovery != transient_recovery
    assert "ORCHESTRATOR_SUPABASE_EMAIL" in permanent_recovery
    assert "backoff" in transient_recovery
    assert "backoff" not in permanent_recovery


def test_backoff_sequence_doubles_until_the_cap_and_resets_when_healthy():
    assert [backoff_seconds(n, 30.0, 900.0) for n in range(0, 8)] == [
        0.0, 30.0, 60.0, 120.0, 240.0, 480.0, 900.0, 900.0,
    ]


def test_a_permanent_400_stops_the_loop_and_records_the_stopped_reason(
    initialized, monkeypatch
):
    _configured(monkeypatch)
    _enable_sync()
    calls: list[str] = []
    reached = asyncio.Event()
    error = SyncPermanentError(
        "Supabase returned HTTP 400: invalid version transition: 0 -> 3",
        status_code=400,
        detail={"recovery": VERSION_REJECTION_RECOVERY},
    )
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(error, calls, reached), poll_seconds=0.01
    )

    asyncio.run(_run_until(reached, runner))

    with session_scope() as session:
        status = runner.status(session)
        health = sync_service.health(session)
    assert calls == ["SyncPermanentError"]  # no second attempt
    assert health["stopped"] is True
    assert health["last_failure_kind"] == "permanent"
    assert "invalid version transition: 0 -> 3" in health["stopped_reason"]
    assert status["consecutive_failures"] == 1
    assert status["stopped_reason"] == health["stopped_reason"]


def test_transient_failures_back_off_without_ever_stopping(initialized, monkeypatch):
    """A capped delay, not a capped attempt count: an offline device must self-heal."""
    _configured(monkeypatch)
    _enable_sync()
    with session_scope() as session:
        settings_service.update(session, {"supabase_sync_interval_seconds": 15})
    calls: list[str] = []
    client = FailingClient(SyncTransientError("Supabase returned HTTP 503: unavailable"), calls)
    runner = SyncRunner(
        transport_factory=lambda: client,
        poll_seconds=0.01,
        backoff_base_seconds=30.0,
        backoff_max_seconds=50.0,
    )

    observed = []
    for _ in range(4):
        with pytest.raises(SyncTransientError):
            asyncio.run(runner.run_once())
        with session_scope() as session:
            health = sync_service.health(session)
        observed.append(
            (
                health["consecutive_failures"],
                health["backoff_seconds"],
                health["stopped"],
                health["next_retry_at"] is not None,
            )
        )

    assert observed == [
        (1, 30.0, False, True),
        (2, 50.0, False, True),
        (3, 50.0, False, True),
        (4, 50.0, False, True),
    ]
    with session_scope() as session:
        health = sync_service.health(session)
    assert health["last_failure_kind"] == "transient"
    assert health["stopped_reason"] is None
    assert calls == ["SyncTransientError"] * 4


def test_default_transient_retries_reach_the_900_second_cap(initialized, monkeypatch):
    """Under the shipping defaults the first retry waits an interval and 900 s is reachable."""
    _configured(monkeypatch)
    _enable_sync()
    calls: list[str] = []
    client = FailingClient(SyncTransientError("Supabase returned HTTP 503: unavailable"), calls)
    runner = SyncRunner(transport_factory=lambda: client, poll_seconds=0.01)

    observed = []
    for _ in range(6):
        with pytest.raises(SyncTransientError):
            asyncio.run(runner.run_once())
        with session_scope() as session:
            health = sync_service.health(session)
        observed.append(health["backoff_seconds"])
    assert observed == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0]
    with session_scope() as session:
        assert sync_service.health(session)["stopped"] is False


def test_the_running_loop_retries_a_transient_failure_after_backoff(
    initialized, monkeypatch
):
    """The loop itself (not just repeated run_once calls) waits and retries a 5xx."""
    _configured(monkeypatch)
    _enable_sync()
    # The retry base is floored at the configured interval, so shrink the interval too; the
    # production default (60 s) is asserted separately by the 900 s cap test.
    real_get = settings_service.get

    def fast_interval(session, key):
        if key == "supabase_sync_interval_seconds":
            return 0.02
        return real_get(session, key)

    monkeypatch.setattr(settings_service, "get", fast_interval)
    calls: list[str] = []
    reached = asyncio.Event()
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(
            SyncTransientError("Supabase returned HTTP 503: unavailable"), calls, reached
        ),
        poll_seconds=0.005,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
    )

    async def scenario():
        task = asyncio.create_task(runner.run_loop())
        await asyncio.wait_for(reached.wait(), timeout=2)
        for _ in range(200):
            if len(calls) >= 2:
                break
            await asyncio.sleep(0.005)
        with session_scope() as session:
            health = sync_service.health(session)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        return health

    health = asyncio.run(scenario())
    assert len(calls) >= 2  # the transient failure was retried by the loop
    assert health["stopped"] is False
    assert health["consecutive_failures"] >= 2
    assert health["backoff_seconds"] == 0.02


def test_identical_failures_log_once_with_an_occurrence_counter(initialized, caplog):
    calls: list[str] = []
    client = FailingClient(SyncTransientError("Supabase returned HTTP 503: unavailable"), calls)
    runner = SyncRunner(transport_factory=lambda: client)

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for _ in range(2):
            with pytest.raises(SyncTransientError):
                asyncio.run(runner.run_once())
        errors = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "occurrence 1" in errors[0].getMessage()
        with session_scope() as session:
            health = sync_service.health(session)
        assert health["log_occurrences"] == 2

        # A different signature is a new failure, not a repeat: it logs once more.
        client.error = SyncTransientError("Supabase request failed: timeout")
        with pytest.raises(SyncTransientError):
            asyncio.run(runner.run_once())
        errors = [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.ERROR]
        assert len(errors) == 2
        with session_scope() as session:
            health = sync_service.health(session)
        assert health["log_occurrences"] == 1


def test_push_sends_synced_version_plus_one_not_the_local_revision(
    initialized, tmp_path
):
    """The exact 0 -> 3 shape: a local revision of 3 must present remote version 1."""
    (tmp_path / "project").mkdir()
    bodies: list[dict] = []

    def handler(request):
        if request.url.path == "/auth/v1/token":
            return httpx.Response(200, json={
                "access_token": "access-test-token",
                "refresh_token": "refresh-test-token",
                "expires_in": 3600,
            })
        if request.url.path == "/rest/v1/rpc/orchestrator_sync_push":
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json={"status": "ok", "record": {}})
        return httpx.Response(404)

    with session_scope() as session:
        project = project_service.create(
            session, ProjectCreate(name="Drifted", path=str(tmp_path / "project"))
        )
        sync_service.scan_local(session)
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        mapping.version = 3  # legacy local drift, remote still has nothing
        mapping.synced_version = 0
        mapping.dirty = True
        session.add(mapping)
        session.commit()
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = SupabaseSyncClient(_settings(), http)
        asyncio.run(client.push(session))
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        assert mapping.synced_version == 1
        assert mapping.version == 1
        asyncio.run(http.aclose())

    project_body = next(b for b in bodies if b["p_entity_type"] == "project")
    assert project_body["p_expected_version"] == 0
    assert project_body["p_version"] == 1


def test_reinit_dry_run_then_apply_realigns_a_rejected_version(initialized, tmp_path):
    """A remote version the local side disagrees with is recovered by reinit, then pushes."""
    (tmp_path / "project").mkdir()
    pushed: list[dict] = []

    def handler(request):
        if request.url.path == "/auth/v1/token":
            return httpx.Response(200, json={
                "access_token": "access-test-token",
                "refresh_token": "refresh-test-token",
                "expires_in": 3600,
            })
        if request.url.path == "/rest/v1/orchestrator_sync_records":
            return httpx.Response(200, json=[{
                "entity_type": "project", "entity_id": entity_id, "version": 1,
            }])
        if request.url.path == "/rest/v1/rpc/orchestrator_sync_push":
            body = json.loads(request.content)
            pushed.append(body)
            return httpx.Response(200, json={"status": "ok", "record": {}})
        return httpx.Response(404)

    with session_scope() as session:
        project = project_service.create(
            session, ProjectCreate(name="RemoteWins", path=str(tmp_path / "project"))
        )
        sync_service.scan_local(session)
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        mapping.synced_version = 3  # the remote actually holds version 1
        mapping.version = 4
        mapping.dirty = True
        session.add(mapping)
        session.commit()
        entity_id = mapping.uuid
        sync_service.write_health(
            session, consecutive_failures=2, stopped=True,
            stopped_reason="remote rejected version",
        )
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = SupabaseSyncClient(_settings(), http)

        versions = asyncio.run(client.remote_versions())
        assert versions == {entity_id: 1}

        plan = sync_service.reinitialise(session, versions, dry_run=True)
        assert plan["dry_run"] is True and plan["change_count"] >= 1
        unchanged = sync_service.get_mapping(session, "project", str(project.id))
        assert (unchanged.synced_version, unchanged.version) == (3, 4)
        assert sync_service.health(session)["stopped"] is True

        applied = sync_service.reinitialise(session, versions, dry_run=False)
        assert applied["dry_run"] is False and applied["health_cleared"] is True
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        assert (mapping.synced_version, mapping.version) == (1, 2)
        assert sync_service.health(session)["stopped"] is False

        asyncio.run(client.push(session))
        assert sync_service.get_mapping(session, "project", str(project.id)).synced_version == 2
        asyncio.run(http.aclose())

    project_body = next(b for b in pushed if b["p_entity_type"] == "project")
    assert project_body["p_expected_version"] == 1
    assert project_body["p_version"] == 2


def test_reinit_never_rebaselines_a_dirty_row_past_unseen_remote_changes(
    initialized, tmp_path
):
    """Reinit must not let a local dirty payload overwrite a newer remote version."""
    (tmp_path / "project").mkdir()
    with session_scope() as session:
        project = project_service.create(
            session, ProjectCreate(name="RemoteAhead", path=str(tmp_path / "project"))
        )
        sync_service.scan_local(session)
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        mapping.synced_version = 1
        mapping.version = 2
        mapping.dirty = True
        session.add(mapping)
        session.commit()
        entity_id = mapping.uuid
        sync_service.write_health(session, consecutive_failures=1, stopped=True,
                                  stopped_reason="conflict")

        plan = sync_service.reinitialise(
            session, {entity_id: 3}, dry_run=False
        )
        mapping = sync_service.get_mapping(session, "project", str(project.id))
        assert plan["remote_ahead_skipped"] == 1
        assert plan["health_cleared"] is False
        assert (mapping.synced_version, mapping.version) == (1, 2)
        assert sync_service.health(session)["stopped"] is True


def test_an_enabled_sync_that_has_never_run_initialises_and_succeeds(
    initialized, monkeypatch
):
    _configured(monkeypatch)
    _enable_sync()
    with session_scope() as session:
        assert sync_service.health(session)["stopped"] is False
        assert sync_service.health(session)["consecutive_failures"] == 0

    calls: list[str] = []
    reached = asyncio.Event()
    runner = SyncRunner(
        transport_factory=lambda: SuccessClient(calls, reached), poll_seconds=0.01
    )
    asyncio.run(_run_until(reached, runner))

    with session_scope() as session:
        health = sync_service.health(session)
        status = runner.status(session)
    assert calls == ["sync"]
    assert health["consecutive_failures"] == 0 and health["stopped"] is False
    assert status["stopped"] is False and status["last_success_at"] is not None


def test_a_stopped_sync_emits_one_sync_failed_notification_per_streak(
    initialized, monkeypatch
):
    _configured(monkeypatch)
    _enable_sync()
    with session_scope() as session:
        settings_service.update(
            session, {"notifications_enabled": True, "notification_sync_failed": True}
        )
    calls: list[str] = []
    reached = asyncio.Event()
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(
            SyncPermanentError("Supabase returned HTTP 400: invalid version transition: 0 -> 3"),
            calls,
            reached,
        ),
        poll_seconds=0.01,
    )
    asyncio.run(_run_until(reached, runner))

    with session_scope() as session:
        rows = list(session.exec(
            select(Notification).where(Notification.trigger == "sync_failed")
        ))
    assert [(row.event, row.status) for row in rows] == [("sync_failed", "failed")]
    assert calls == ["SyncPermanentError"]


def test_a_manual_sync_that_stops_permanently_still_notifies(initialized, monkeypatch):
    """The loop skips notification while stopped, so run_once itself must emit the alert."""
    _configured(monkeypatch)
    _enable_sync()
    with session_scope() as session:
        settings_service.update(
            session, {"notifications_enabled": True, "notification_sync_failed": True}
        )
    calls: list[str] = []
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(
            SyncPermanentError("Supabase returned HTTP 400: invalid version transition: 0 -> 3"),
            calls,
        )
    )
    with pytest.raises(SyncPermanentError):
        asyncio.run(runner.run_once())
    with pytest.raises(SyncPermanentError):
        asyncio.run(runner.run_once())

    with session_scope() as session:
        rows = list(session.exec(
            select(Notification).where(Notification.trigger == "sync_failed")
        ))
    assert [(row.event, row.status) for row in rows] == [("sync_failed", "failed")]
    assert calls == ["SyncPermanentError"] * 2


def test_a_persistently_failing_transient_sync_notifies_once(initialized, monkeypatch):
    _configured(monkeypatch)
    _enable_sync()
    with session_scope() as session:
        settings_service.update(
            session,
            {
                "notifications_enabled": True,
                "notification_sync_failed": True,
                "supabase_sync_interval_seconds": 15,
            },
        )
    calls: list[str] = []
    client = FailingClient(SyncTransientError("Supabase returned HTTP 503: unavailable"), calls)
    runner = SyncRunner(transport_factory=lambda: client, backoff_base_seconds=1.0)
    for _ in range(4):
        with pytest.raises(SyncTransientError):
            asyncio.run(runner.run_once())

    with session_scope() as session:
        rows = list(session.exec(
            select(Notification).where(Notification.trigger == "sync_failed")
        ))
        health = sync_service.health(session)
    assert [(row.event, row.status) for row in rows] == [("sync_failed", "failed")]
    assert health["streak_notified"] is True
    assert health["stopped"] is False


def test_a_failed_emission_releases_the_claim_so_a_later_cycle_notifies(
    initialized, monkeypatch
):
    """An emission that raises must leave the streak unannounced, not silently marked done."""
    _configured(monkeypatch)
    _enable_sync()
    from orchestrator.services import log_service

    emitted: list[str] = []
    real_log_event = log_service.log_event

    def flaky_log_event(*args, **kwargs):
        if not emitted:
            emitted.append("raised")
            raise RuntimeError("notification fan-out failed")
        emitted.append("delivered")
        return real_log_event(*args, **kwargs)

    monkeypatch.setattr(log_service, "log_event", flaky_log_event)
    calls: list[str] = []
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(
            SyncPermanentError(
                "Supabase returned HTTP 400: invalid version transition: 0 -> 3"
            ),
            calls,
        )
    )

    with pytest.raises(SyncPermanentError):
        asyncio.run(runner.run_once())
    with session_scope() as session:
        health = sync_service.health(session)
    assert health["stop_notified"] is False
    assert health["stop_notified_claim"] is None
    assert "notification fan-out failed" in health["last_notify_error"]

    # The next cycle may try again because the claim was released, and this time it lands.
    with pytest.raises(SyncPermanentError):
        asyncio.run(runner.run_once())
    with session_scope() as session:
        health = sync_service.health(session)
    assert health["stop_notified"] is True
    assert health["stop_notified_claim"] is None
    assert health["last_notify_error"] is None
    assert emitted == ["raised", "delivered"]


def test_an_emission_failure_does_not_mask_the_sync_exception(initialized, monkeypatch):
    """The caller must still see the original classified failure, unchanged."""
    _configured(monkeypatch)
    _enable_sync()
    from orchestrator.services import log_service

    def raising_log_event(*_args, **_kwargs):
        raise RuntimeError("notification fan-out failed")

    monkeypatch.setattr(log_service, "log_event", raising_log_event)
    error = SyncPermanentError(
        "Supabase returned HTTP 400: invalid version transition: 0 -> 3"
    )
    runner = SyncRunner(transport_factory=lambda: FailingClient(error, []))

    with pytest.raises(SyncPermanentError) as caught:
        asyncio.run(runner.run_once())
    assert caught.value is error
    assert str(caught.value) == "Supabase returned HTTP 400: invalid version transition: 0 -> 3"


def test_a_completed_emission_sets_the_flag_exactly_once(initialized):
    with session_scope() as session:
        token = sync_service.claim_health_flag(session, "stop_notified")
    assert token

    with session_scope() as session:
        assert sync_service.complete_health_flag(session, "stop_notified", token) is True
    with session_scope() as session:
        # A second terminal write with the same token matches nothing: the claim is gone.
        assert sync_service.complete_health_flag(session, "stop_notified", token) is False
        health = sync_service.health(session)
    assert health["stop_notified"] is True
    assert health["stop_notified_claim"] is None


def test_the_stopped_loop_retries_a_notification_whose_emission_failed(
    initialized, monkeypatch
):
    """A released claim must be retried by the loop, which otherwise makes no sync attempts."""
    _configured(monkeypatch)
    _enable_sync()
    from orchestrator.services import log_service

    emitted: list[str] = []
    real_log_event = log_service.log_event

    def flaky_log_event(*args, **kwargs):
        if not emitted:
            emitted.append("raised")
            raise RuntimeError("notification fan-out failed")
        emitted.append("delivered")
        return real_log_event(*args, **kwargs)

    monkeypatch.setattr(log_service, "log_event", flaky_log_event)
    # The notify retry runs on its own capped backoff now, so shrink the base/ceiling the way the
    # transient-retry loop test does; the production defaults are asserted by the 900 s cap test.
    real_get = settings_service.get

    def fast_interval(session, key):
        if key == "supabase_sync_interval_seconds":
            return 0.02
        return real_get(session, key)

    monkeypatch.setattr(settings_service, "get", fast_interval)
    calls: list[str] = []
    reached = asyncio.Event()
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(
            SyncPermanentError(
                "Supabase returned HTTP 400: invalid version transition: 0 -> 3"
            ),
            calls,
            reached,
        ),
        poll_seconds=0.01,
        backoff_base_seconds=0.01,
        backoff_max_seconds=0.02,
    )

    asyncio.run(_run_until(reached, runner))

    with session_scope() as session:
        health = sync_service.health(session)
    assert emitted == ["raised", "delivered"]
    assert health["stop_notified"] is True
    assert health["stop_notified_claim"] is None
    # Still exactly one sync attempt: the retry is the alert, not the refused request.
    assert calls == ["SyncPermanentError"]


def test_two_overlapping_streaks_produce_one_notification(initialized, monkeypatch):
    """While one emission is in flight the leased claim blocks the second emitter."""
    from orchestrator.services import log_service

    real_log_event = log_service.log_event
    emitted: list[str] = []
    in_flight = threading.Event()
    release = threading.Event()

    def slow_log_event(*args, **kwargs):
        emitted.append("sync_failed")
        in_flight.set()
        release.wait(timeout=5)
        return real_log_event(*args, **kwargs)

    monkeypatch.setattr(log_service, "log_event", slow_log_event)
    health = {
        "stopped": True,
        "consecutive_failures": 1,
        "stopped_reason": "permanent client error: invalid version transition: 0 -> 3",
    }
    runner = SyncRunner(transport_factory=lambda: FailingClient(SyncPermanentError("x"), []))

    first: list[bool] = []
    first_thread = threading.Thread(target=lambda: first.append(runner._notify_if_needed(health)))
    first_thread.start()
    assert in_flight.wait(timeout=5)

    # The first emission owns a live claim, so the overlapping streak must write nothing.
    second = runner._notify_if_needed(health)
    release.set()
    first_thread.join(timeout=5)

    assert first == [True]
    assert second is False
    assert emitted == ["sync_failed"]
    with session_scope() as session:
        assert sync_service.health(session)["stop_notified"] is True


def test_repeated_notify_failures_log_once_with_an_occurrence_counter(initialized, monkeypatch, caplog):
    """DoD 5 applies to the alert path: a broken notifier logs once, then only counts."""
    _configured(monkeypatch)
    _enable_sync()
    from orchestrator.services import log_service

    def raising_log_event(*_args, **_kwargs):
        raise RuntimeError("notification fan-out failed")

    monkeypatch.setattr(log_service, "log_event", raising_log_event)
    calls: list[str] = []
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(
            SyncPermanentError(
                "Supabase returned HTTP 400: invalid version transition: 0 -> 3"
            ),
            calls,
        )
    )

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        for _ in range(3):
            with pytest.raises(SyncPermanentError):
                asyncio.run(runner.run_once())

    notify_errors = [
        record
        for record in caplog.records
        if record.name == LOGGER
        and record.levelno == logging.ERROR
        and "notification path failed" in record.getMessage()
    ]
    assert len(notify_errors) == 1
    assert "occurrence 1" in notify_errors[0].getMessage()
    with session_scope() as session:
        health = sync_service.health(session)
    assert health["notify_failures"] == 3
    assert health["notify_log_occurrences"] == 3
    assert health["last_notify_error"] == "notification fan-out failed"


def test_notify_failures_back_off_to_the_configured_ceiling(initialized):
    """The alert retry delay doubles from the same base and caps at the same ceiling."""
    with session_scope() as session:
        settings_service.update(session, {"supabase_sync_interval_seconds": 15})
    runner = SyncRunner(backoff_base_seconds=30.0, backoff_max_seconds=50.0)

    observed = []
    for _ in range(5):
        runner._record_notify_failure(RuntimeError("notification fan-out failed"))
        with session_scope() as session:
            observed.append(sync_service.health(session)["notify_backoff_seconds"])
    assert observed == [30.0, 50.0, 50.0, 50.0, 50.0]


def test_status_surfaces_a_failed_alert(initialized, monkeypatch):
    """`sync status` / the GUI must show a failed alert, not only the log (DoD 3/4)."""
    _configured(monkeypatch)
    _enable_sync()
    from orchestrator.services import log_service

    def raising_log_event(*_args, **_kwargs):
        raise RuntimeError("notification fan-out failed")

    monkeypatch.setattr(log_service, "log_event", raising_log_event)
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(SyncPermanentError("permanent 400"), [])
    )

    with pytest.raises(SyncPermanentError):
        asyncio.run(runner.run_once())
    with session_scope() as session:
        status = runner.status(session)
    assert status["last_notify_error"] == "notification fan-out failed"
    assert status["last_notify_failed_at"] is not None
    assert status["notify_failures"] == 1


def test_a_successful_emission_resets_the_notify_failure_counter(initialized, monkeypatch):
    """A delivered alert is the reset signal for the dedupe counter and the retry schedule."""
    _configured(monkeypatch)
    _enable_sync()
    from orchestrator.services import log_service

    real_log_event = log_service.log_event
    emitted: list[str] = []

    def flaky_log_event(*args, **kwargs):
        if not emitted:
            emitted.append("raised")
            raise RuntimeError("notification fan-out failed")
        emitted.append("delivered")
        return real_log_event(*args, **kwargs)

    monkeypatch.setattr(log_service, "log_event", flaky_log_event)
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(SyncPermanentError("permanent 400"), [])
    )

    with pytest.raises(SyncPermanentError):
        asyncio.run(runner.run_once())
    with session_scope() as session:
        failed = sync_service.health(session)
    with pytest.raises(SyncPermanentError):
        asyncio.run(runner.run_once())
    with session_scope() as session:
        delivered = sync_service.health(session)
        status = runner.status(session)

    assert failed["notify_failures"] == 1
    assert delivered["notify_log_occurrences"] == 0
    assert delivered["notify_next_retry_at"] is None
    assert delivered["last_notify_error"] is None
    assert status["last_notify_error"] is None
    assert emitted == ["raised", "delivered"]


def test_the_stopped_loop_respects_the_alert_backoff_before_retrying(
    initialized, monkeypatch
):
    """A persistently failing notifier is retried on its backoff, not on every poll."""
    _configured(monkeypatch)
    _enable_sync()
    from orchestrator.services import log_service

    def raising_log_event(*_args, **_kwargs):
        raise RuntimeError("notification fan-out failed")

    monkeypatch.setattr(log_service, "log_event", raising_log_event)
    calls: list[str] = []
    reached = asyncio.Event()
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(
            SyncPermanentError(
                "Supabase returned HTTP 400: invalid version transition: 0 -> 3"
            ),
            calls,
            reached,
        ),
        poll_seconds=0.01,
        # A 0.5 s alert backoff is far longer than the observation window, so only the emission
        # from the failing cycle itself may happen. Retrying every poll would be many more.
        backoff_base_seconds=0.5,
        backoff_max_seconds=0.5,
    )

    asyncio.run(_run_until(reached, runner))

    with session_scope() as session:
        health = sync_service.health(session)
    assert health["notify_failures"] == 1
    assert health["stop_notified"] is False
    assert calls == ["SyncPermanentError"]


def test_a_stopped_sync_respects_the_notification_master_switch(initialized, monkeypatch):
    _configured(monkeypatch)
    _enable_sync()
    calls: list[str] = []
    reached = asyncio.Event()
    runner = SyncRunner(
        transport_factory=lambda: FailingClient(
            SyncPermanentError("Supabase returned HTTP 400: invalid version transition: 0 -> 3"),
            calls,
            reached,
        ),
        poll_seconds=0.01,
    )
    asyncio.run(_run_until(reached, runner))

    with session_scope() as session:
        rows = list(session.exec(
            select(Notification).where(Notification.trigger == "sync_failed")
        ))
    assert [(row.status, row.reason) for row in rows] == [("skipped", "master_disabled")]
    assert settings_service.defaults()["notification_sync_failed"] is True


# ---------------------------------------------------------------- cross-process health writes
#
# The desktop loop and a CLI `sync now` are separate processes sharing one SQLite file. These
# tests force the interleaving a read-modify-write loses: both writers read the same bytes before
# either commits. A plain "read, update, write" fails them; the compare-and-set path passes.


def _race(two_writers):
    """Run two session-per-writer threads, each released by the same barrier."""
    barrier = threading.Barrier(2)
    results: list[object] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(two_writers(barrier))
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not errors, errors
    assert not any(thread.is_alive() for thread in threads)
    return results


def test_concurrent_failure_records_cannot_lose_an_increment(initialized):
    """Both writers compute failures=old+1 from the same snapshot; the CAS must still yield 2."""
    with session_scope() as session:
        sync_service.write_health(session, consecutive_failures=0)

    def record(barrier: threading.Barrier) -> dict:
        with session_scope() as session:
            first = {"seen": False}

            def mutate(current: dict) -> dict:
                if not first["seen"]:
                    first["seen"] = True
                    # Both threads are here with the same bytes read: the lost-update window.
                    barrier.wait(timeout=5)
                return {"consecutive_failures": int(current.get("consecutive_failures") or 0) + 1}

            return sync_service.update_health(session, mutate)

    results = _race(record)
    assert sorted(r["consecutive_failures"] for r in results) == [1, 2]
    with session_scope() as session:
        assert sync_service.health(session)["consecutive_failures"] == 2


def test_a_health_flag_claim_is_won_by_exactly_one_racer(initialized, monkeypatch):
    """Two processes that both read the flag as false must not both alert."""
    with session_scope() as session:
        sync_service.write_health(session, stop_notified=False)

    original = sync_service._read_health_raw
    barrier = threading.Barrier(2)
    local = threading.local()
    stale: list[tuple] = []
    with session_scope() as session:
        stale.append(original(session))

    def stale_first_read(session):
        # Force the exact pre-CAS shape: every racer's first read returns the same old bytes.
        if not getattr(local, "used", False):
            local.used = True
            barrier.wait(timeout=5)
            return stale[0]
        return original(session)

    monkeypatch.setattr(sync_service, "_read_health_raw", stale_first_read)

    def claim(_barrier: threading.Barrier) -> str | None:
        with session_scope() as session:
            return sync_service.claim_health_flag(session, "stop_notified")

    results = _race(claim)
    winners = [token for token in results if token is not None]
    assert len(winners) == 1
    assert sum(1 for token in results if token is None) == 1
    with session_scope() as session:
        _exists, _raw, stored = original(session)
    assert stored["stop_notified"] is False
    assert stored["stop_notified_claim"]["token"] == winners[0]
