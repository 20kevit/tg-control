"""Reject malformed adapter failures without leaking data or retrying effects."""

import asyncio
import uuid

import pytest
from sqlalchemy import select
from test_core import action, batch, setup

from tg_control.adapters.base import AdapterError, RateLimited, SessionStatus, SessionUnavailable
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.core import BatchRejected, Gateway
from tg_control.db import AuditEvent


def mutated_error():
    error = AdapterError("timeout")
    error.code = "synthetic-upstream-secret"
    error.outcome_unknown = "synthetic-upstream-secret"
    return error


def assert_no_secret(database, result):
    assert "synthetic-upstream-secret" not in str(result)
    with database() as session:
        events = session.scalars(select(AuditEvent)).all()
        assert all("synthetic-upstream-secret" not in str(vars(event)) for event in events)


def test_exception_constructors_reject_invalid_types():
    for value in (
        True,
        "synthetic-upstream-secret",
        None,
        -1,
        float("nan"),
        float("inf"),
        10**10000,
    ):
        with pytest.raises(ValueError):
            RateLimited(value)
    with pytest.raises(ValueError):
        SessionUnavailable("synthetic-upstream-secret")
    assert AdapterError(["synthetic-upstream-secret"]).code == "adapter_failure"
    assert AdapterError("timeout", outcome_unknown="invalid").outcome_unknown is True


@pytest.mark.asyncio
async def test_malformed_preflight_error_does_not_leak_or_execute(database):
    account, _, _, principal = setup(database)

    class BadLookup(SyntheticAdapter):
        async def get_chat(self, *_args):
            raise mutated_error()

    gateway = Gateway(database, BadLookup())
    with pytest.raises(BatchRejected) as caught:
        await gateway.execute(
            principal,
            batch(action(account, "send", "public-channel", text="no effect"), key=uuid.uuid4()),
        )
    assert caught.value.status_code == 503
    assert caught.value.items[0]["error"] == "adapter_failure"
    assert_no_secret(database, caught.value.items)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["rate_limit", "session"])
async def test_uncertain_failure_never_retries_after_synthetic_effect(database, kind):
    account, _, _, principal = setup(database)

    class AppliedThenUncertain(SyntheticAdapter):
        send_calls = 0
        reconnect_calls = 0

        async def execute(self, account_id, operation, chat_id, params):
            if operation != "send":
                return await super().execute(account_id, operation, chat_id, params)
            self.send_calls += 1
            result = await super().execute(account_id, operation, chat_id, params)
            if self.send_calls == 1:
                error = RateLimited(0) if kind == "rate_limit" else SessionUnavailable(False)
                error.outcome_unknown = True
                raise error
            return result

        async def reconnect(self, account_id):
            self.reconnect_calls += 1
            return await super().reconnect(account_id)

    adapter = AppliedThenUncertain()
    gateway = Gateway(database, adapter, timeout_seconds=1)
    result = await asyncio.wait_for(
        gateway.execute(
            principal,
            batch(action(account, "send", "public-channel", text="one effect"), key=uuid.uuid4()),
        ),
        timeout=2,
    )
    item = result["items"][0]
    assert item["status"] == "failure" and item["error"] == "adapter_failure"
    assert item["outcome_unknown"] is True
    assert adapter.send_calls == 1 and adapter.reconnect_calls == 0
    history = await adapter.execute(account, "history", "public-channel", {})
    assert sum(message["text"] == "one effect" for message in history["messages"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delay",
    [-1, float("nan"), float("inf"), True, "synthetic-upstream-secret", 10**10000],
    ids=["negative", "nan", "infinite", "bool", "text", "too-large"],
)
async def test_mutated_retry_delay_is_not_used_or_exposed(database, delay):
    account, _, _, principal = setup(database)

    class BadRateLimit(SyntheticAdapter):
        calls = 0

        async def execute(self, account_id, operation, chat_id, params):
            if operation != "send":
                return await super().execute(account_id, operation, chat_id, params)
            self.calls += 1
            error = RateLimited(0)
            error.retry_after = delay
            raise error

    adapter = BadRateLimit()
    result = await asyncio.wait_for(
        Gateway(database, adapter, timeout_seconds=0.05).execute(
            principal,
            batch(action(account, "send", "public-channel", text="no retry"), key=uuid.uuid4()),
        ),
        timeout=2,
    )
    item = result["items"][0]
    assert item["error"] == "adapter_failure" and item["outcome_unknown"] is True
    assert "retry_after" not in item and adapter.calls == 1
    assert_no_secret(database, result)


@pytest.mark.asyncio
async def test_mutated_execution_error_is_sanitized_and_unknown(database):
    account, _, _, principal = setup(database)
    adapter = SyntheticAdapter()
    adapter.inject_fault(account, "send", mutated_error())
    result = await Gateway(database, adapter).execute(
        principal,
        batch(action(account, "send", "public-channel", text="synthetic"), key=uuid.uuid4()),
    )
    assert result["items"][0]["error"] == "adapter_failure"
    assert result["items"][0]["outcome_unknown"] is True
    assert_no_secret(database, result)


@pytest.mark.asyncio
async def test_mutated_delivery_error_never_leaks_read_result(database):
    account, _, _, principal = setup(database)

    class BadDelivery(SyntheticAdapter):
        lookup_calls = 0

        async def get_chat(self, *args):
            self.lookup_calls += 1
            if self.lookup_calls == 4:
                raise mutated_error()
            return await super().get_chat(*args)

    result = await Gateway(database, BadDelivery()).execute(
        principal, batch(action(account, "history", "public-channel"))
    )
    assert result["items"][0]["status"] == "failure"
    assert result["items"][0]["error"] == "adapter_failure"
    assert "data" not in result["items"][0]
    assert_no_secret(database, result)


@pytest.mark.asyncio
async def test_owner_status_and_reconnect_sanitize_mutated_errors(database):
    account, _, _, _ = setup(database)

    class BadOwnerStatus(SyntheticAdapter):
        async def status(self, _account_id):
            raise mutated_error()

        async def reconnect(self, _account_id):
            raise mutated_error()

    gateway = Gateway(database, BadOwnerStatus())
    status = await gateway.owner_account_status(account)
    reconnect = await gateway.owner_account_status(account, reconnect=True)
    assert status == {
        "state": "adapter_failure",
        "intervention_required": True,
        "outcome_unknown": False,
    }
    assert reconnect == {
        "state": "adapter_failure",
        "intervention_required": True,
        "outcome_unknown": True,
    }
    assert_no_secret(database, [status, reconnect])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reconnect_status", "intervention"),
    [
        (SessionStatus("reauth_required", True), True),
        (SessionStatus("disconnected", False), False),
        (SessionStatus("initializing", False), False),
        (SessionStatus("initializing", True), True),
    ],
)
async def test_reconnect_requires_ready_before_retrying_original_operation(
    database, reconnect_status, intervention
):
    account, _, _, _ = setup(database)
    attempts = []

    class StillUnavailable(SyntheticAdapter):
        async def reconnect(self, _account_id):
            return reconnect_status

    async def operation():
        attempts.append(1)
        raise SessionUnavailable(False)

    gateway = Gateway(database, StillUnavailable())
    with pytest.raises(SessionUnavailable) as caught:
        await gateway._call(account, operation, mutation=True)
    assert caught.value.intervention_required is intervention
    assert attempts == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "field", "value"),
    [
        ("rate", "code", "synthetic-upstream-secret"),
        ("rate", "outcome_unknown", "synthetic-upstream-secret"),
        ("session", "code", "synthetic-upstream-secret"),
        ("session", "intervention_required", "synthetic-upstream-secret"),
        ("session", "outcome_unknown", "synthetic-upstream-secret"),
    ],
)
async def test_contradictory_rejection_metadata_never_triggers_retry(database, kind, field, value):
    account, _, _, principal = setup(database)

    class BadRejection(SyntheticAdapter):
        calls = 0

        async def execute(self, account_id, operation, chat_id, params):
            if operation != "send":
                return await super().execute(account_id, operation, chat_id, params)
            self.calls += 1
            error = RateLimited(0) if kind == "rate" else SessionUnavailable(False)
            setattr(error, field, value)
            raise error

    adapter = BadRejection()
    result = await Gateway(database, adapter).execute(
        principal,
        batch(action(account, "send", "public-channel", text="synthetic"), key=uuid.uuid4()),
    )
    item = result["items"][0]
    assert item["error"] == "adapter_failure" and item["outcome_unknown"] is True
    assert adapter.calls == 1
    assert_no_secret(database, result)


@pytest.mark.asyncio
async def test_owner_and_monitor_reject_malformed_session_exception(database):
    account, _, _, _ = setup(database)

    class BadSession(SyntheticAdapter):
        async def status(self, _account_id):
            error = SessionUnavailable(False)
            error.intervention_required = "synthetic-upstream-secret"
            raise error

        async def reconnect(self, _account_id):
            error = SessionUnavailable(False)
            error.outcome_unknown = True
            raise error

    gateway = Gateway(database, BadSession())
    status = await gateway.owner_account_status(account)
    reconnect = await gateway.owner_account_status(account, reconnect=True)
    await gateway.maintain_sessions()
    assert status == {
        "state": "adapter_failure",
        "intervention_required": True,
        "outcome_unknown": False,
    }
    assert reconnect == {
        "state": "adapter_failure",
        "intervention_required": True,
        "outcome_unknown": True,
    }
    with database() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.event == "session_transition")
        ).all()
        assert all(event.error_code == "adapter_failure" for event in events)
    assert_no_secret(database, [status, reconnect])


def test_http_and_mcp_do_not_expose_mutated_adapter_failure(api, settings, monkeypatch):
    from test_api import create_resources, execute, set_policy
    from test_mcp import rpc

    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    adapter = api.app.state.gateway.adapter
    adapter.inject_fault(account["id"], "send", mutated_error())
    response = execute(
        api,
        client["credential"],
        account,
        "send",
        "public-channel",
        key=uuid.uuid4(),
        text="synthetic",
    )
    assert response.json()["items"][0]["error"] == "adapter_failure"
    assert response.json()["items"][0]["outcome_unknown"] is True
    assert "synthetic-upstream-secret" not in response.text
    adapter.inject_fault(account["id"], "send", mutated_error())
    mcp = rpc(
        api,
        client["credential"],
        "tools/call",
        {
            "name": "tg_execute",
            "arguments": {
                "batch": {
                    "idempotency_key": str(uuid.uuid4()),
                    "actions": [
                        {
                            "account_id": account["id"],
                            "operation": "send",
                            "chat_id": "public-channel",
                            "text": "synthetic",
                        }
                    ],
                }
            },
        },
    )
    assert mcp.json()["result"]["isError"] is True
    assert mcp.json()["result"]["structuredContent"]["items"][0]["error"] == "adapter_failure"
    assert "synthetic-upstream-secret" not in mcp.text

    async def bad_status(_account_id):
        raise mutated_error()

    monkeypatch.setattr(adapter, "status", bad_status)
    status = api.get(f"/v1/admin/accounts/{account['id']}/status", headers=owner)
    assert status.json()["state"] == "adapter_failure"
    assert "synthetic-upstream-secret" not in status.text
    events = api.get("/v1/admin/audit", headers=owner).text
    assert "synthetic-upstream-secret" not in events


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["generic", "malformed_flag"])
async def test_generic_or_malformed_write_failure_is_conservatively_unknown(database, error_kind):
    account, _, _, principal = setup(database)
    adapter = SyntheticAdapter()
    error = AdapterError("adapter_failure" if error_kind == "generic" else "timeout")
    if error_kind == "malformed_flag":
        error.outcome_unknown = "synthetic-upstream-secret"
    adapter.inject_fault(account, "send", error)
    result = await Gateway(database, adapter).execute(
        principal,
        batch(action(account, "send", "public-channel", text="synthetic"), key=uuid.uuid4()),
    )
    assert result["items"][0]["error"] == "adapter_failure"
    assert result["items"][0]["outcome_unknown"] is True
    assert_no_secret(database, result)
