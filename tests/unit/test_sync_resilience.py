"""T-41: version protocol, permanent/transient classification, backoff, dedupe, and reinit.

Every test is offline: the transport is a fake client or an httpx.MockTransport. No test opens a
network connection or talks to a real Supabase project.
"""

from __future__ import annotations

import asyncio
import json
import logging
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
    SupabaseSyncClient,
    SyncPermanentError,
    SyncTransientError,
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
    assert permanent.detail["recovery"] == "orchestrator sync reinit --dry-run"

    for status_code in (500, 503, 408, 429):
        transient = SupabaseSyncClient._response_error(response(status_code, "unavailable"))
        assert isinstance(transient, SyncTransientError)
        assert transient.detail["kind"] == "transient"
        assert transient.detail["retryable"] is True


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
        detail={"recovery": "orchestrator sync reinit --dry-run"},
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
