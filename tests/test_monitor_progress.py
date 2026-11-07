"""Owner-visible, process-local session monitor progress; synthetic data only."""

import asyncio
import threading
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from test_api import auth, create_resources
from test_migrations import migrate

from tg_control.api import create_app
from tg_control.monitoring import SessionMonitorProgress


def test_progress_records_failure_recovery_and_monotonic_age():
    current = [10.0]
    timestamp = datetime(2026, 11, 6, tzinfo=timezone.utc)
    progress = SessionMonitorProgress(clock=lambda: current[0], wall_clock=lambda: timestamp)
    initial = progress.snapshot(task_running=False)
    assert initial["last_result"] == "never" and initial["last_success_at"] is None
    progress.begin()
    current[0] = 12.5
    running = progress.snapshot(task_running=True)
    assert running["pass_in_progress"] is True and running["current_pass_seconds"] == 2.5
    progress.finish(success=False)
    progress.begin()
    progress.finish(success=False)
    failed = progress.snapshot(task_running=True)
    assert failed["last_result"] == "failure" and failed["consecutive_failures"] == 2
    progress.begin()
    progress.finish(success=True)
    current[0] = 16.5
    recovered = progress.snapshot(task_running=True)
    assert recovered["last_result"] == "success" and recovered["consecutive_failures"] == 0
    assert recovered["last_success_at"] == timestamp.isoformat()
    assert recovered["seconds_since_success"] == 4.0
    assert recovered["current_pass_seconds"] is None


def test_owner_can_inspect_monitor_failure_recovery_and_stopped_task(settings, monkeypatch, caplog):
    migrate(settings.database_url)
    monkeypatch.setattr("tg_control.api.SESSION_MONITOR_INTERVAL_SECONDS", 0.02)
    app = create_app(settings)
    failed, retry_started, recovered = threading.Event(), threading.Event(), threading.Event()
    release = asyncio.Event()
    calls = []

    async def flaky_monitor():
        calls.append(1)
        if len(calls) == 1:
            failed.set()
            raise RuntimeError("synthetic-monitor-private-detail")
        if len(calls) == 2:
            retry_started.set()
            await release.wait()
            recovered.set()
            return
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.gateway, "maintain_sessions", flaky_monitor)
    with TestClient(app) as api:
        assert failed.wait(3)
        path = "/v1/admin/session-monitor"
        owner = auth(settings.owner_token)
        first = api.get(path, headers=owner)
        assert first.status_code == 200
        assert first.json()["task_running"] is True
        assert first.json()["last_result"] == "failure"
        assert first.json()["consecutive_failures"] == 1
        assert "synthetic-monitor-private-detail" not in first.text
        owner, _account, client = create_resources(api, settings)
        assert api.get(path).status_code == 401
        assert api.get(path, headers=auth(client["credential"])).status_code == 401
        assert retry_started.wait(3)
        api.portal.call(release.set)
        assert recovered.wait(3)
        second = api.get(path, headers=owner).json()
        assert second["last_result"] == "success" and second["consecutive_failures"] == 0
        assert second["last_success_at"] and second["seconds_since_success"] is not None

        async def cancel_monitor():
            task = app.state.session_monitor_task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        api.portal.call(cancel_monitor)
        stopped = api.get(path, headers=owner).json()
        assert stopped["task_running"] is False
        history = api.get("/v1/admin/audit?event=view_session_monitor", headers=owner).json()
        assert len(history["events"]) == 3
        assert "synthetic-monitor-private-detail" not in str(history)
    assert "session monitoring failed" in caplog.text
    assert "synthetic-monitor-private-detail" not in caplog.text
    with TestClient(create_app(settings)) as restarted:
        progress = restarted.get("/v1/admin/session-monitor", headers=owner).json()
        assert progress["task_running"] is True
        assert progress["last_result"] in {"never", "success"}
        assert progress["consecutive_failures"] == 0


def test_owner_can_see_an_in_progress_monitor_pass(settings, monkeypatch):
    migrate(settings.database_url)
    app = create_app(settings)
    entered = threading.Event()

    async def stalled_monitor():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(app.state.gateway, "maintain_sessions", stalled_monitor)
    with TestClient(app) as api:
        assert entered.wait(3)
        result = api.get("/v1/admin/session-monitor", headers=auth(settings.owner_token)).json()
        assert result["task_running"] is True and result["pass_in_progress"] is True
        assert result["last_result"] == "never" and result["current_pass_seconds"] >= 0
