import asyncio
from contextlib import suppress

import pytest

from orchestrator.core.config import reset_settings_cache
from orchestrator.core.db import session_scope
from orchestrator.services import settings_service
from orchestrator.services.sync_runner import SyncBusyError, SyncRunner


class FakeClient:
    def __init__(self, calls, on_sync=None):
        self.calls = calls
        self.on_sync = on_sync

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def sync_once(self, _session):
        self.calls.append("sync")
        if self.on_sync is not None:
            await self.on_sync()
        return {
            "pulled": {"applied": 1},
            "pushed": {"pushed": 2},
            "status": {"tracked": 3, "pending": 0, "conflicts": 0, "synced": 3},
        }


def test_manual_run_works_while_automatic_sync_is_disabled(initialized):
    calls = []
    runner = SyncRunner(transport_factory=lambda: FakeClient(calls))
    with session_scope() as session:
        assert runner.status(session)["enabled"] is False
        result = asyncio.run(runner.run_once())
        status = runner.status(session)
    assert calls == ["sync"]
    assert result["pushed"]["pushed"] == 2
    assert status["last_success_at"] is not None
    assert status["last_error"] is None
    assert status["running"] is False


def test_overlapping_manual_runs_are_rejected(initialized):
    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def pause():
            entered.set()
            await release.wait()

        calls = []
        runner = SyncRunner(transport_factory=lambda: FakeClient(calls, pause))
        first = asyncio.create_task(runner.run_once())
        await asyncio.wait_for(entered.wait(), timeout=1)
        with pytest.raises(SyncBusyError):
            await runner.run_once()
        release.set()
        await first
        assert calls == ["sync"]

    asyncio.run(scenario())


def test_enabled_but_unconfigured_background_sync_makes_no_transport_calls(
    initialized, monkeypatch
):
    for name in (
        "ORCHESTRATOR_SUPABASE_URL",
        "ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY",
        "ORCHESTRATOR_SUPABASE_EMAIL",
        "ORCHESTRATOR_SUPABASE_PASSWORD",
    ):
        monkeypatch.delenv(name, raising=False)
    reset_settings_cache()
    with session_scope() as session:
        settings_service.update(session, {"supabase_sync_enabled": True})

    async def scenario():
        calls = []
        runner = SyncRunner(transport_factory=lambda: FakeClient(calls), poll_seconds=0.01)
        task = asyncio.create_task(runner.run_loop())
        await asyncio.sleep(0.04)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert calls == []

    asyncio.run(scenario())


def test_enabled_and_configured_background_sync_runs_without_network(
    initialized, monkeypatch
):
    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_PUBLISHABLE_KEY", "publishable-test-key")
    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_EMAIL", "user@example.test")
    monkeypatch.setenv("ORCHESTRATOR_SUPABASE_PASSWORD", "local-test-password")
    reset_settings_cache()
    with session_scope() as session:
        settings_service.update(session, {"supabase_sync_enabled": True})

    async def scenario():
        completed = asyncio.Event()

        async def finish():
            completed.set()

        calls = []
        runner = SyncRunner(transport_factory=lambda: FakeClient(calls, finish), poll_seconds=0.01)
        task = asyncio.create_task(runner.run_loop())
        await asyncio.wait_for(completed.wait(), timeout=1)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert calls == ["sync"]
        with session_scope() as session:
            status = runner.status(session)
        assert status["configured"] is True
        assert status["enabled"] is True
        assert status["last_success_at"] is not None

    asyncio.run(scenario())
