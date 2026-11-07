import asyncio
import base64
import uuid

import pytest
from sqlalchemy import select

from tg_control.adapters.base import RateLimited
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.auth import authenticate, revoke_client, rotate_credential
from tg_control.core import BatchRejected, Gateway
from tg_control.db import Account, AuditEvent, Client, Grant
from tg_control.policy import GrantInput, Scope
from tg_control.schemas import Batch


def setup(database, grants=None):
    with database() as session:
        a = Account(alias="one", phone_encrypted="synthetic")
        b = Account(alias="two", phone_encrypted="synthetic")
        c = Client(name="client")
        session.add_all([a, b, c])
        session.flush()
        policy = grants or GrantInput(
            read=Scope(
                enabled=True,
                allow_types=["public_channel", "public_group"],
                exclude_chats=["public-group"],
            ),
            write=Scope(enabled=True, allow_chats=["public-channel"]),
            admin=Scope(enabled=True, allow_chats=["private-group"]),
        )
        session.add(
            Grant(
                client_id=c.id,
                account_id=a.id,
                read_scope=policy.read.model_dump(mode="json"),
                write_scope=policy.write.model_dump(mode="json"),
                admin_scope=policy.admin.model_dump(mode="json"),
            )
        )
        token = rotate_credential(session, c.id)
        session.commit()
        principal = authenticate(session, token)
        return a.id, b.id, c.id, principal


def batch(*actions, key=None):
    return Batch.model_validate(
        {"actions": list(actions), "idempotency_key": str(key) if key else None}
    )


def action(account, op, chat=None, **kwargs):
    return {"account_id": account, "operation": op, **({"chat_id": chat} if chat else {}), **kwargs}


@pytest.mark.asyncio
async def test_discovery_and_search_filter_exclusions_and_accounts(database):
    a, b, _, principal = setup(database)
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter)
    assert [x["id"] for x in await gateway.list_accounts(principal)] == [a]
    result = await gateway.execute(
        principal, batch(action(a, "list_chats"), action(a, "search", query="fixture"))
    )
    assert [c["id"] for c in result["items"][0]["data"]["chats"]] == ["public-channel"]
    found = result["items"][1]["data"]["chats"]
    assert [c["chat_id"] for c in found] == ["public-channel"]
    assert found[0]["history_complete"] is False
    with pytest.raises(BatchRejected) as exc:
        await gateway.execute(
            principal,
            batch(action(a, "search", query="fixture"), action(b, "search", query="fixture")),
        )
    assert exc.value.status_code == 403
    assert [x["status"] for x in exc.value.items] == ["not_executed", "denied"]


@pytest.mark.asyncio
async def test_pre_authorize_all_and_forward_source_prevents_exfiltration(database):
    a, _, _, principal = setup(database)
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter)
    request = batch(
        action(a, "send", "public-channel", text="must not appear"),
        action(a, "forward", "public-channel", source_chat_id="private-direct", message_id=1),
        key=uuid.uuid4(),
    )
    with pytest.raises(BatchRejected) as exc:
        await gateway.execute(principal, request)
    assert exc.value.status_code == 403
    history = await adapter.execute(a, "history", "public-channel", {})
    assert all(m["text"] != "must not appear" for m in history["messages"])


@pytest.mark.asyncio
async def test_independent_admin_write_and_read(database):
    a, _, _, principal = setup(database)
    gateway = Gateway(database, SyntheticAdapter())
    # Admin does not grant Write or Read for this private group.
    for op, kwargs in [("history", {}), ("send", {"text": "x"})]:
        with pytest.raises(BatchRejected):
            await gateway.execute(
                principal, batch(action(a, op, "private-group", **kwargs), key=uuid.uuid4())
            )
    result = await gateway.execute(
        principal,
        batch(
            action(a, "set_title", "private-group", title="Synthetic renamed"),
            action(a, "set_member_status", "private-group", member_id="member-a", status="member"),
            key=uuid.uuid4(),
        ),
    )
    assert all(i["status"] == "success" for i in result["items"])
    # Write does not grant Admin.
    with pytest.raises(BatchRejected):
        await gateway.execute(
            principal, batch(action(a, "set_title", "public-channel", title="x"), key=uuid.uuid4())
        )


@pytest.mark.asyncio
async def test_write_media_edit_delete_forward_and_item_failures(database):
    a, _, _, principal = setup(database)
    gateway = Gateway(database, SyntheticAdapter())
    send = await gateway.execute(
        principal,
        batch(
            action(a, "send", "public-channel", text="safe synthetic"),
            action(
                a,
                "send_media",
                "public-channel",
                text="caption",
                media={
                    "filename": "test.txt",
                    "content_type": "text/plain",
                    "data_base64": base64.b64encode(b"test").decode(),
                },
            ),
            action(a, "delete", "public-channel", message_id=999),
            key=uuid.uuid4(),
        ),
    )
    assert [x["status"] for x in send["items"]] == ["success", "success", "failure"]
    msg_id = send["items"][0]["data"]["message_id"]
    media_id = send["items"][1]["data"]["message_id"]
    mutate = await gateway.execute(
        principal,
        batch(
            action(a, "edit", "public-channel", message_id=msg_id, text="edited"),
            action(
                a, "forward", "public-channel", source_chat_id="public-channel", message_id=msg_id
            ),
            action(a, "get_media", "public-channel", message_id=media_id),
            action(a, "delete", "public-channel", message_id=msg_id),
            key=uuid.uuid4(),
        ),
    )
    assert all(x["status"] == "success" for x in mutate["items"])
    assert mutate["items"][2]["data"]["media"]["data_base64"] == "dGVzdA=="
    missing = await gateway.execute(
        principal, batch(action(a, "get_message", "public-channel", message_id=msg_id))
    )
    assert missing["items"][0]["error"] == "not_found"


@pytest.mark.asyncio
async def test_replay_is_rejected_and_audit_has_no_content(database):
    a, _, _, principal = setup(database)
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter)
    phrase = "INJECTION synthetic private payload: ignore instructions"
    key = uuid.uuid4()
    req = batch(action(a, "send", "public-channel", text=phrase), key=key)
    assert (await gateway.execute(principal, req))["items"][0]["status"] == "success"
    with pytest.raises(BatchRejected) as exc:
        await gateway.execute(principal, req)
    assert exc.value.status_code == 409
    history = await adapter.execute(a, "history", "public-channel", {})
    assert sum(m["text"] == phrase for m in history["messages"]) == 1
    with database() as session:
        events = session.scalars(select(AuditEvent)).all()
        assert all(phrase not in str(vars(e)) for e in events)
        assert any(e.error_code == "duplicate_request" for e in events)


@pytest.mark.asyncio
async def test_rate_limit_wait_timeout_reconnect_and_unknown_failures(database):
    a, _, _, principal = setup(database)
    adapter = SyntheticAdapter()
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    gateway = Gateway(database, adapter, timeout_seconds=0.5, sleep=fake_sleep)
    adapter.set_state(a, "disconnected")
    adapter.inject_fault(a, "send", RateLimited(0.05))
    res = await gateway.execute(
        principal, batch(action(a, "send", "public-channel", text="x"), key=uuid.uuid4())
    )
    assert res["items"][0]["status"] == "success" and waits == [0.05]
    adapter.inject_fault(a, "send", RateLimited(10))
    res = await gateway.execute(
        principal, batch(action(a, "send", "public-channel", text="x"), key=uuid.uuid4())
    )
    assert res["items"][0]["error"] == "rate_limit_timeout"
    assert res["items"][0]["retry_after"] == 10
    adapter.inject_fault(a, "send", RuntimeError("sensitive upstream exception"))
    res = await gateway.execute(
        principal, batch(action(a, "send", "public-channel", text="x"), key=uuid.uuid4())
    )
    assert res["items"][0]["error"] == "adapter_failure" and res["items"][0]["outcome_unknown"]
    assert "sensitive upstream exception" not in str(res)
    adapter.set_state(a, "reauth_required")
    with pytest.raises(BatchRejected) as exc:
        await gateway.execute(principal, batch(action(a, "history", "public-channel")))
    assert exc.value.status_code == 503
    assert exc.value.items[0]["error"] == "reauth_required"


@pytest.mark.asyncio
async def test_revocation_during_wait_stops_retry(database):
    a, _, client_id, principal = setup(database)
    adapter = SyntheticAdapter()

    async def revoke_during_sleep(_):
        with database() as session:
            revoke_client(session, client_id)
            session.commit()

    gateway = Gateway(database, adapter, sleep=revoke_during_sleep)
    adapter.inject_fault(a, "send", RateLimited(0.01))
    result = await gateway.execute(
        principal, batch(action(a, "send", "public-channel", text="no"), key=uuid.uuid4())
    )
    assert result["items"][0]["error"] == "credential_revoked"
    history = await adapter.execute(a, "history", "public-channel", {})
    assert all(m["text"] != "no" for m in history["messages"])


@pytest.mark.asyncio
async def test_scope_revocation_during_search_retry_does_not_leak(database):
    a, _, client_id, principal = setup(database)
    adapter = SyntheticAdapter()

    async def exclude_during_sleep(_):
        with database() as session:
            grant = session.get(Grant, (client_id, a))
            scope = dict(grant.read_scope)
            scope["exclude_chats"] = ["public-channel", "public-group"]
            grant.read_scope = scope
            session.commit()

    gateway = Gateway(database, adapter, sleep=exclude_during_sleep)
    adapter.inject_fault(a, "search", RateLimited(0.01))
    result = await gateway.execute(principal, batch(action(a, "search", query="fixture")))
    assert result["items"][0]["status"] == "failure"
    assert result["items"][0]["error"] == "denied"
    assert "messages" not in str(result)


@pytest.mark.asyncio
async def test_audit_failure_prevents_effects(database, monkeypatch):
    a, _, _, principal = setup(database)
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter)
    from tg_control import audit

    def fail(*args, **kwargs):
        raise RuntimeError("audit storage failed")

    monkeypatch.setattr(audit, "record", fail)
    with pytest.raises(RuntimeError):
        await gateway.execute(
            principal, batch(action(a, "send", "public-channel", text="no"), key=uuid.uuid4())
        )
    history = await adapter.execute(a, "history", "public-channel", {})
    assert all(m["text"] != "no" for m in history["messages"])


@pytest.mark.asyncio
async def test_timeout_of_inflight_mutation_is_unknown(database):
    a, _, _, principal = setup(database)

    class SlowAdapter(SyntheticAdapter):
        async def execute(self, *args, **kwargs):
            await asyncio.sleep(0.2)
            return await super().execute(*args, **kwargs)

    result = await Gateway(database, SlowAdapter(), timeout_seconds=0.01).execute(
        principal, batch(action(a, "send", "public-channel", text="x"), key=uuid.uuid4())
    )
    assert result["items"][0]["error"] == "timeout"
    assert result["items"][0]["outcome_unknown"] is True


@pytest.mark.asyncio
async def test_two_clients_two_accounts_and_authorized_cross_account_search(database):
    a, b, client_id, principal = setup(database)
    with database() as session:
        first_grant = session.get(Grant, (client_id, a))
        session.add(
            Grant(
                client_id=client_id,
                account_id=b,
                read_scope=dict(first_grant.read_scope),
                write_scope=Scope().model_dump(mode="json"),
                admin_scope=Scope().model_dump(mode="json"),
            )
        )
        other = Client(name="other")
        session.add(other)
        session.flush()
        session.add(
            Grant(
                client_id=other.id,
                account_id=b,
                read_scope=dict(first_grant.read_scope),
                write_scope=Scope().model_dump(mode="json"),
                admin_scope=Scope().model_dump(mode="json"),
            )
        )
        token = rotate_credential(session, other.id)
        session.commit()
        second = authenticate(session, token)
    gateway = Gateway(database, SyntheticAdapter())
    result = await gateway.execute(
        principal, batch(action(a, "search", query="fixture"), action(b, "search", query="fixture"))
    )
    assert len(result["items"]) == 2
    a_text = result["items"][0]["data"]["chats"][0]["messages"][0]["text"]
    b_text = result["items"][1]["data"]["chats"][0]["messages"][0]["text"]
    assert a in a_text and b not in a_text and b in b_text and a not in b_text
    with pytest.raises(BatchRejected):
        await gateway.execute(second, batch(action(a, "history", "public-channel")))
    assert (await gateway.execute(second, batch(action(b, "history", "public-channel"))))["items"][
        0
    ]["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["history", "search"])
async def test_revocation_while_read_in_flight_never_returns_content(database, operation):
    account, _, client_id, principal = setup(database)
    started, release = asyncio.Event(), asyncio.Event()

    class SlowAdapter(SyntheticAdapter):
        async def execute(self, account_id, name, chat_id, params):
            result = await super().execute(account_id, name, chat_id, params)
            if name == operation:
                started.set()
                await release.wait()
            return result

    gateway = Gateway(database, SlowAdapter())
    request = (
        action(account, "history", "public-channel")
        if operation == "history"
        else action(account, "search", query="fixture")
    )
    pending = asyncio.create_task(gateway.execute(principal, batch(request)))
    await started.wait()
    with database() as session:
        grant = session.get(Grant, (client_id, account))
        grant.read_scope = {**grant.read_scope, "exclude_chats": ["public-channel"]}
        session.commit()
    release.set()
    result = await pending
    assert result["items"][0]["status"] == "failure"
    assert result["items"][0]["error"] == "denied"
    assert "messages" not in str(result)


@pytest.mark.asyncio
async def test_scope_revoked_during_chat_lookup_does_not_authorize(database):
    account, _, client_id, principal = setup(database)
    started, release = asyncio.Event(), asyncio.Event()

    class SlowLookup(SyntheticAdapter):
        async def get_chat(self, account_id, chat_id):
            chat = await super().get_chat(account_id, chat_id)
            started.set()
            await release.wait()
            return chat

    gateway = Gateway(database, SlowLookup())
    pending = asyncio.create_task(
        gateway.execute(principal, batch(action(account, "history", "public-channel")))
    )
    await started.wait()
    with database() as session:
        grant = session.get(Grant, (client_id, account))
        grant.read_scope = {**grant.read_scope, "exclude_chats": ["public-channel"]}
        session.commit()
    release.set()
    with pytest.raises(BatchRejected) as error:
        await pending
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_earlier_read_in_batch_is_not_delivered_after_later_revocation(database):
    account, _, client_id, principal = setup(database)
    started, release = asyncio.Event(), asyncio.Event()

    class SlowSend(SyntheticAdapter):
        async def execute(self, account_id, operation, chat_id, params):
            if operation == "send":
                started.set()
                await release.wait()
            return await super().execute(account_id, operation, chat_id, params)

    gateway = Gateway(database, SlowSend())
    request = batch(
        action(account, "history", "public-channel"),
        action(account, "send", "public-channel", text="synthetic"),
        key=uuid.uuid4(),
    )
    pending = asyncio.create_task(gateway.execute(principal, request))
    await started.wait()
    with database() as session:
        grant = session.get(Grant, (client_id, account))
        grant.read_scope = {**grant.read_scope, "exclude_chats": ["public-channel"]}
        session.commit()
    release.set()
    result = await pending
    assert result["items"][0]["status"] == "failure"
    assert result["items"][0]["error"] == "denied"
    assert "data" not in result["items"][0]
    assert result["items"][1]["status"] == "success"


@pytest.mark.asyncio
async def test_search_delivery_filters_chat_revoked_after_earlier_result(database):
    account, _, client_id, principal = setup(database)
    with database() as session:
        grant = session.get(Grant, (client_id, account))
        grant.read_scope = {**grant.read_scope, "exclude_chats": []}
        session.commit()
    started, release = asyncio.Event(), asyncio.Event()

    class SlowSecondChat(SyntheticAdapter):
        async def execute(self, account_id, operation, chat_id, params):
            result = await super().execute(account_id, operation, chat_id, params)
            if operation == "search" and chat_id == "public-group":
                started.set()
                await release.wait()
            return result

    gateway = Gateway(database, SlowSecondChat())
    pending = asyncio.create_task(
        gateway.execute(principal, batch(action(account, "search", query="fixture")))
    )
    await started.wait()
    with database() as session:
        grant = session.get(Grant, (client_id, account))
        grant.read_scope = {**grant.read_scope, "exclude_chats": ["public-channel"]}
        session.commit()
    release.set()
    result = await pending
    assert result["items"][0]["status"] == "success"
    assert [entry["chat_id"] for entry in result["items"][0]["data"]["chats"]] == ["public-group"]
    assert "public-channel" not in str(result)


@pytest.mark.asyncio
async def test_positive_rate_limits_are_retried_beyond_eight_when_deadline_allows(database):
    account, _, _, principal = setup(database)
    adapter = SyntheticAdapter()
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    gateway = Gateway(database, adapter, timeout_seconds=2, sleep=fake_sleep)
    for _ in range(12):
        adapter.inject_fault(account, "send", RateLimited(0.01))
    result = await gateway.execute(
        principal,
        batch(
            action(account, "send", "public-channel", text="synthetic after waits"),
            key=uuid.uuid4(),
        ),
    )
    assert result["items"][0]["status"] == "success"
    assert waits == [0.01] * 12
    history = await adapter.execute(account, "history", "public-channel", {})
    assert sum(item["text"] == "synthetic after waits" for item in history["messages"]) == 1


@pytest.mark.asyncio
async def test_zero_delay_rate_limit_spin_guard_never_causes_effect(database):
    account, _, _, principal = setup(database)
    adapter = SyntheticAdapter()
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    gateway = Gateway(database, adapter, timeout_seconds=2, sleep=fake_sleep)
    for _ in range(9):
        adapter.inject_fault(account, "send", RateLimited(0))
    result = await gateway.execute(
        principal,
        batch(action(account, "send", "public-channel", text="must not send"), key=uuid.uuid4()),
    )
    item = result["items"][0]
    assert item["error"] == "rate_limit_timeout" and item["retry_after"] == 0
    assert item["outcome_unknown"] is False and waits == [0] * 8
    history = await adapter.execute(account, "history", "public-channel", {})
    assert all(message["text"] != "must not send" for message in history["messages"])


@pytest.mark.asyncio
@pytest.mark.parametrize("stall", ["sleep", "reauthorize"])
async def test_retry_wait_and_reauthorization_are_deadline_bounded_without_unknown_effect(
    database, stall
):
    from tg_control.adapters.base import AdapterError

    account, _, _, _ = setup(database)
    calls = []

    async def rejected():
        calls.append("attempt")
        raise RateLimited(0.001)

    async def sleep(_seconds):
        if stall == "sleep":
            await asyncio.Event().wait()

    async def guard():
        if stall == "reauthorize":
            await asyncio.Event().wait()

    gateway = Gateway(database, SyntheticAdapter(), timeout_seconds=0.03, sleep=sleep)
    with pytest.raises(AdapterError) as error:
        await asyncio.wait_for(
            gateway._call(account, rejected, mutation=True, before_retry=guard), timeout=1
        )
    assert error.value.code == ("rate_limit_timeout" if stall == "sleep" else "timeout")
    assert error.value.outcome_unknown is False
    assert calls == ["attempt"]


@pytest.mark.asyncio
async def test_owner_session_status_and_reconnect_are_bounded_and_audited(database):
    account, _, _, _ = setup(database)

    class HungAdapter(SyntheticAdapter):
        reconnect_started = False

        async def status(self, account_id):
            await asyncio.Event().wait()

        async def reconnect(self, account_id):
            self.reconnect_started = True
            await asyncio.Event().wait()

    adapter = HungAdapter()
    gateway = Gateway(database, adapter, timeout_seconds=0.03)
    status = await asyncio.wait_for(gateway.owner_account_status(account), timeout=1)
    assert status == {"state": "timeout", "intervention_required": True, "outcome_unknown": False}
    reconnect = await asyncio.wait_for(
        gateway.owner_account_status(account, reconnect=True), timeout=1
    )
    assert reconnect == {
        "state": "reconnect_timeout",
        "intervention_required": True,
        "outcome_unknown": True,
    }
    assert adapter.reconnect_started
    with database() as session:
        failures = session.scalars(
            select(AuditEvent).where(
                AuditEvent.event.in_(["account_status", "account_reconnect"]),
                AuditEvent.decision == "failure",
            )
        ).all()
    assert {event.error_code for event in failures} == {"timeout", "reconnect_timeout"}


@pytest.mark.asyncio
async def test_owner_session_adapter_errors_are_sanitized_and_fail_closed(database):
    from tg_control.adapters.base import SessionStatus

    account, _, _, _ = setup(database)

    class InvalidStatusAdapter(SyntheticAdapter):
        async def status(self, account_id):
            return SessionStatus("synthetic-private-upstream-text", False)

        async def reconnect(self, account_id):
            raise RuntimeError("synthetic-private-exception")

    gateway = Gateway(database, InvalidStatusAdapter())
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
    with database() as session:
        events = session.scalars(select(AuditEvent)).all()
        assert all("synthetic-private" not in str(vars(event)) for event in events)


@pytest.mark.asyncio
async def test_reauthentication_state_always_requires_owner_intervention(database):
    from tg_control.adapters.base import SessionStatus

    account, _, _, _ = setup(database)

    class InconsistentAdapter(SyntheticAdapter):
        async def status(self, account_id):
            return SessionStatus("reauth_required", intervention_required=False)

    gateway = Gateway(database, InconsistentAdapter())
    assert await gateway.owner_account_status(account) == {
        "state": "reauth_required",
        "intervention_required": True,
    }
