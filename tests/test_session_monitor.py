"""Synthetic session monitoring, audit persistence, and failure handling."""

import asyncio
import threading
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_core import setup
from test_migrations import migrate

from tg_control.adapters.base import AdapterError, SessionStatus, SessionUnavailable
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.api import create_app
from tg_control.core import SESSION_MONITOR_KEY, Gateway
from tg_control.db import Account, AuditEvent, Setting


def transitions(database):
    with database() as session:
        return session.scalars(
            select(AuditEvent)
            .where(AuditEvent.event == "session_transition")
            .order_by(AuditEvent.id)
        ).all()


@pytest.mark.asyncio
async def test_session_transitions_are_durable_deduplicated_and_account_scoped(database):
    first, second, _, _ = setup(database)
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter)
    await gateway.maintain_sessions()
    assert transitions(database) == []  # Initial healthy state is not an alert.
    adapter.set_state(first, "disconnected")
    adapter.set_state(second, "reauth_required")
    # Concurrent maintenance calls in the same worker must not duplicate notifications.
    await asyncio.gather(gateway.maintain_sessions(), gateway.maintain_sessions())
    events = transitions(database)
    assert {(event.account_id, event.decision) for event in events} == {
        (first, "disconnected"),
        (second, "reauth_required"),
    }
    assert all(event.actor_kind == "system" and event.chat_id is None for event in events)
    restarted = Gateway(database, adapter)
    await restarted.maintain_sessions()
    assert len(transitions(database)) == 2
    await adapter.reconnect(first)
    await restarted.maintain_sessions()
    assert [(event.account_id, event.decision) for event in transitions(database)][-1] == (
        first,
        "ready",
    )
    with database() as session:
        session.get(Account, second).enabled = False
        session.commit()
    await restarted.maintain_sessions()
    with database() as session:
        states = session.get(Setting, SESSION_MONITOR_KEY).value["states"]
        assert second not in states and states[first] == "ready"
        session.get(Account, second).enabled = True
        session.commit()
    await restarted.maintain_sessions()
    assert [(event.account_id, event.decision) for event in transitions(database)][-1] == (
        second,
        "reauth_required",
    )


@pytest.mark.asyncio
async def test_monitor_bounds_status_calls_and_sanitizes_errors(database):
    first, second, _, _ = setup(database)

    class FaultyStatus(SyntheticAdapter):
        recovered = False

        async def status(self, account_id):
            if self.recovered:
                return SessionStatus("ready")
            if account_id == first:
                await asyncio.Event().wait()
            raise RuntimeError("synthetic-sensitive-login-detail")

        async def reconnect(self, account_id):
            raise AssertionError("monitor must never reconnect")

    adapter = FaultyStatus()
    gateway = Gateway(database, adapter, timeout_seconds=0.03)
    await asyncio.wait_for(gateway.maintain_sessions(), timeout=1)
    events = transitions(database)
    assert {(event.account_id, event.decision, event.error_code) for event in events} == {
        (first, "unavailable", "timeout"),
        (second, "unavailable", "adapter_failure"),
    }
    assert all("synthetic-sensitive" not in str(vars(event)) for event in events)
    adapter.recovered = True
    await gateway.maintain_sessions()
    assert {(event.account_id, event.decision) for event in transitions(database)[-2:]} == {
        (first, "ready"),
        (second, "ready"),
    }


@pytest.mark.asyncio
async def test_monitor_normalizes_unavailable_and_malformed_statuses(database):
    first, second, _, _ = setup(database)

    class UnavailableStatus(SyntheticAdapter):
        async def status(self, account_id):
            if account_id == first:
                raise SessionUnavailable(True)
            return SessionStatus("ready", intervention_required=True)

    gateway = Gateway(database, UnavailableStatus())
    await gateway.maintain_sessions()
    assert {
        (event.account_id, event.decision, event.error_code) for event in transitions(database)
    } == {
        (first, "reauth_required", "reauth_required"),
        (second, "unavailable", "adapter_failure"),
    }


@pytest.mark.asyncio
async def test_monitor_reapplies_error_allowlist(database):
    setup(database)

    class MutatedError(SyntheticAdapter):
        async def status(self, _account_id):
            error = AdapterError("timeout")
            error.code = "synthetic-secret-in-upstream-error"
            raise error

    await Gateway(database, MutatedError()).maintain_sessions()
    events = transitions(database)
    assert len(events) == 2
    assert all(event.error_code == "adapter_failure" for event in events)
    assert all("synthetic-secret" not in str(vars(event)) for event in events)


@pytest.mark.asyncio
async def test_monitor_rechecks_account_enabled_after_inflight_status(database):
    first, second, _, _ = setup(database)
    entered, release = asyncio.Event(), asyncio.Event()

    class SlowStatus(SyntheticAdapter):
        async def status(self, account_id):
            if account_id == first:
                entered.set()
                await release.wait()
                return SessionStatus("reauth_required", True)
            return SessionStatus("ready")

    gateway = Gateway(database, SlowStatus(), timeout_seconds=1)
    pending = asyncio.create_task(gateway.maintain_sessions())
    await asyncio.wait_for(entered.wait(), timeout=1)
    with database() as session:
        session.get(Account, first).enabled = False
        session.commit()
    release.set()
    await asyncio.wait_for(pending, timeout=1)
    assert transitions(database) == []
    with database() as session:
        assert session.get(Setting, SESSION_MONITOR_KEY).value["states"] == {second: "ready"}


@pytest.mark.asyncio
async def test_failed_audit_does_not_advance_monitor_state(database, monkeypatch):
    first, _, _, _ = setup(database)
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter)
    await gateway.maintain_sessions()
    adapter.set_state(first, "reauth_required")
    with monkeypatch.context() as failing:

        def unavailable(*_args, **_kwargs):
            raise RuntimeError("synthetic-audit-failure")

        failing.setattr("tg_control.core.audit.record", unavailable)
        with pytest.raises(RuntimeError):
            await gateway.maintain_sessions()
    with database() as session:
        assert session.get(Setting, SESSION_MONITOR_KEY).value["states"][first] == "ready"
    assert transitions(database) == []
    await gateway.maintain_sessions()
    assert [(event.account_id, event.decision) for event in transitions(database)] == [
        (first, "reauth_required")
    ]


@pytest.mark.asyncio
async def test_monitor_rejects_corrupt_persisted_state(database):
    account, _, _, _ = setup(database)
    with database() as session:
        session.add(Setting(key=SESSION_MONITOR_KEY, value={"states": {account: ["bad"]}}))
        session.commit()
    gateway = Gateway(database, SyntheticAdapter())
    with pytest.raises(ValueError, match="invalid session monitor state"):
        await gateway.maintain_sessions()
    with database() as session:
        assert session.get(Setting, SESSION_MONITOR_KEY).value["states"][account] == ["bad"]


@pytest.mark.asyncio
async def test_monitor_checks_all_accounts_with_bounded_concurrency(database):
    first, second, _, _ = setup(database)
    with database() as session:
        for index in range(9):
            session.add(
                Account(id=str(uuid.uuid4()), alias=f"extra-{index}", phone_encrypted="synthetic")
            )
        session.commit()

    class ConcurrentStatus(SyntheticAdapter):
        active = 0
        peak = 0

        async def status(self, account_id):
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                await asyncio.sleep(0.01)
                return SessionStatus("reauth_required", True)
            finally:
                self.active -= 1

    adapter = ConcurrentStatus()
    gateway = Gateway(database, adapter, timeout_seconds=1)
    await gateway.maintain_sessions()
    events = transitions(database)
    assert len(events) == 11 and {first, second} <= {event.account_id for event in events}
    assert 1 < adapter.peak <= 8


def test_background_monitor_retries_sanitizes_and_cancels_on_shutdown(
    settings, monkeypatch, caplog
):
    migrate(settings.database_url)
    monkeypatch.setattr("tg_control.api.SESSION_MONITOR_INTERVAL_SECONDS", 0.02)
    app = create_app(settings)
    retried, cancelled = threading.Event(), threading.Event()
    calls = []

    async def flaky_monitor():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("synthetic-monitor-secret")
        retried.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(app.state.gateway, "maintain_sessions", flaky_monitor)
    with TestClient(app):
        assert retried.wait(3)
    assert cancelled.wait(1) and len(calls) == 2
    assert "session monitoring failed" in caplog.text
    assert "synthetic-monitor-secret" not in caplog.text


def test_filtered_session_history_is_owner_only_and_sanitized(api, settings):
    from test_api import auth, create_resources

    owner, account, client = create_resources(api, settings)
    api.app.state.gateway.adapter.set_state(account["id"], "reauth_required")
    api.portal.call(api.app.state.gateway.maintain_sessions)
    path = "/v1/admin/audit?event=session_transition&limit=5"
    assert api.get(path).status_code == 401
    assert api.get(path, headers=auth(client["credential"])).status_code == 401
    response = api.get(path, headers=owner)
    assert response.status_code == 200
    events = response.json()["events"]
    assert len(events) == 1 and events[0]["account_id"] == account["id"]
    assert events[0]["decision"] == "reauth_required"
    assert client["credential"] not in response.text and settings.owner_token not in response.text
    invalid = api.get("/v1/admin/audit?event=%3Csynthetic%3E", headers=owner)
    assert invalid.status_code == 422 and "synthetic" not in invalid.text
    before = api.get(f"{path}&before_id={events[0]['id']}", headers=owner).json()
    assert before["events"] == []
