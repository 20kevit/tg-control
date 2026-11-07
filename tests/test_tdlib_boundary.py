"""Offline TDLib boundary: no network, no credentials, bounded lifecycle."""

import socket

import pytest

from tg_control.adapters.base import AdapterError
from tg_control.adapters.tdlib_boundary import (
    ALLOWED_TRANSITIONS,
    AUTHORIZATION_UPDATE_MAP,
    CREDENTIAL_STATES,
    ComplianceBlocked,
    OfflineTdjsonTransport,
    OfflineTdlibAdapter,
    TdlibMode,
    build_adapter,
)

ACCOUNT = "6b7c5d9e-1f2a-4b3c-8d5e-7f8a9b0c1d2e"
PUBLIC = {
    "@type": "chat",
    "id": 777,
    "title": "synthetic public channel",
    "has_protected_content": False,
    "message_auto_delete_time": 0,
    "type": {"@type": "chatTypeSupergroup", "supergroup_id": 42, "is_channel": True},
}
SUPERGROUP = {
    "@type": "supergroup",
    "id": 42,
    "has_location": False,
    "is_channel": True,
    "is_direct_messages_group": False,
    "is_administered_direct_messages_group": False,
    "usernames": {"@type": "usernames", "active_usernames": ["synthetic"]},
}
PROTECTED = {**PUBLIC, "id": 778, "has_protected_content": True}


def message(message_id, chat_id=777, text="synthetic"):
    return {
        "@type": "message",
        "id": message_id,
        "chat_id": chat_id,
        "can_be_saved": True,
        "self_destruct_type": None,
        "ephemeral_content": None,
        "restriction_info": None,
        "sending_state": None,
        "scheduling_state": None,
        "self_destruct_in": 0,
        "auto_delete_in": 0,
        "ephemeral_message_id": 0,
        "content": {
            "@type": "messageText",
            "text": {"@type": "formattedText", "text": text, "entities": []},
        },
    }


async def ready_adapter():
    transport = OfflineTdjsonTransport()
    adapter = OfflineTdlibAdapter(transport, synthetic_account_id=ACCOUNT)
    await adapter.synthetic_authorize("synthetic:fixture")
    await adapter.register_chat(PUBLIC, SUPERGROUP)
    return adapter, transport


def test_live_mode_fails_closed_without_override(monkeypatch):
    with pytest.raises(ComplianceBlocked):
        build_adapter("live")
    with pytest.raises(ComplianceBlocked):
        OfflineTdlibAdapter(OfflineTdjsonTransport(), mode=TdlibMode.LIVE)
    for name in ("TG_TDLIB_LIVE", "TG_CONTROL_LIVE", "TG_ALLOW_LIVE"):
        monkeypatch.setenv(name, "1")
    with pytest.raises(ComplianceBlocked):
        build_adapter("live")
    adapter = build_adapter()
    assert adapter.authorization_state == "wait_parameters"
    monkeypatch.delenv("TG_TDLIB_LIVE")


async def test_offline_flow_makes_no_network_calls(monkeypatch):
    def blocked(*_args, **_kwargs):  # noqa: ANN001 - test helper
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    adapter, transport = await ready_adapter()
    assert hasattr(adapter, "drain_updates")  # UpdateSource shape
    await adapter.feed_update(
        {
            "@type": "updateNewMessage",
            "@extra": "u1",
            "message": message(10, text="synthetic hello"),
        }
    )
    drained = await adapter.drain_updates(ACCOUNT)
    assert [update.payload["text"] for update in drained] == ["synthetic hello"]
    history = await adapter.execute(ACCOUNT, "history", "777", {"limit": 10})
    assert [message["message_id"] for message in history["messages"]] == [10]
    assert transport.outbox == []


async def test_authorization_state_machine_rejects_credentials_and_live_states():
    adapter = OfflineTdlibAdapter(OfflineTdjsonTransport(), synthetic_account_id=ACCOUNT)
    with pytest.raises(AdapterError) as early:
        await adapter.register_chat(PUBLIC, SUPERGROUP)
    assert early.value.code == "disconnected"
    with pytest.raises(AdapterError):
        await adapter.synthetic_authorize("real:+15550100")
    for state in sorted(CREDENTIAL_STATES):
        with pytest.raises(AdapterError) as credential:
            await adapter.apply_authorization_update(
                {"@type": "updateAuthorizationState", "authorization_state": {"@type": state}}
            )
        assert credential.value.code in {"reauth_required", "unsupported_operation"}
        assert adapter.authorization_state == "reauth_required"
        await adapter.close()
        adapter = OfflineTdlibAdapter(OfflineTdjsonTransport(), synthetic_account_id=ACCOUNT)
    with pytest.raises(AdapterError):
        await adapter.apply_authorization_update(
            {"@type": "updateAuthorizationState", "authorization_state": {"@type": "mystery"}}
        )
    assert adapter.authorization_state == "reauth_required"
    closed = OfflineTdlibAdapter(OfflineTdjsonTransport(), synthetic_account_id=ACCOUNT)
    await closed.synthetic_authorize("synthetic:fixture")
    await closed.close()
    with pytest.raises(AdapterError) as gone:
        await closed.status(ACCOUNT)
    assert gone.value.code == "reauth_required"
    assert not ALLOWED_TRANSITIONS[__import__(
        "tg_control.adapters.tdlib_boundary", fromlist=["AuthorizationState"]
    ).AuthorizationState.CLOSED]


async def test_updates_are_deduplicated_ordered_bounded_and_scoped():
    adapter, _ = await ready_adapter()
    raw = {
        "@type": "updateNewMessage",
        "@extra": "dup-1",
        "message": message(1, text="first"),
    }
    assert await adapter.feed_update(raw) is not None
    assert await adapter.feed_update(raw) is None  # exact duplicate upstream id
    assert await adapter.feed_update({"@type": "updateUserStatus", "@extra": "u2"}) is None
    assert await adapter.feed_update({"@type": "updateNewMessage", "message": None}) is None
    assert await adapter.feed_update(
        {
            "@type": "updateNewMessage",
            "@extra": "unknown-chat",
            "message": message(2, chat_id=9_999, text="unknown"),
        }
    ) is None
    with pytest.raises(AdapterError) as protected:
        await adapter.register_chat(PROTECTED, {**SUPERGROUP, "id": 43})
    assert protected.value.code == "denied"
    update = await adapter.feed_update(
        {
            "@type": "updateNewMessage",
            "@extra": "second",
            "message": message(4, text="second"),
        }
    )
    assert update.payload["sequence"] == 2
    drained = await adapter.drain_updates(ACCOUNT, 10)
    assert [item.payload["message_id"] for item in drained] == [1, 4]
    assert await adapter.drain_updates(ACCOUNT, 10) == []
    for index in range(600):
        adapter._updates.append(update)
    assert len(adapter._updates) == 500  # bounded buffer


async def test_reads_paginate_and_refuse_mutations_or_unsafe_chats():
    adapter, _ = await ready_adapter()
    for message_id in range(1, 6):
        await adapter.feed_update(
            {
                "@type": "updateNewMessage",
                "@extra": f"m{message_id}",
                "message": message(message_id, text=f"message {message_id}"),
            }
        )
    first = await adapter.execute(ACCOUNT, "history", "777", {"limit": 2})
    assert [item["message_id"] for item in first["messages"]] == [5, 4]
    second = await adapter.execute(ACCOUNT, "history", "777", {"limit": 2, "before_id": 4})
    assert [item["message_id"] for item in second["messages"]] == [3, 2]
    for operation in ("send", "delete", "forward", "get_media", "list_chats"):
        with pytest.raises(AdapterError) as refused:
            await adapter.execute(ACCOUNT, operation, "777", {"text": "x"})
        assert refused.value.code == "unsupported_operation"
    with pytest.raises(AdapterError) as not_found:
        await adapter.execute(ACCOUNT, "history", "1", {"limit": 5})
    assert not_found.value.code == "not_found"
    with pytest.raises(AdapterError):
        await adapter.execute(ACCOUNT, "history", "777", {"limit": 500})
    fetched = await adapter.execute(ACCOUNT, "get_message", "777", {"message_id": 1})
    assert fetched["message"]["text"] == "message 1"
    with pytest.raises(AdapterError) as missing:
        await adapter.execute(ACCOUNT, "get_message", "777", {"message_id": 99})
    assert missing.value.code == "not_found"
    with pytest.raises(AdapterError) as no_self:
        await adapter.self_chat(ACCOUNT)
    assert no_self.value.code == "unsupported_operation"


async def test_reconnect_requires_fresh_synthetic_fixture():
    adapter = OfflineTdlibAdapter(OfflineTdjsonTransport(), synthetic_account_id=ACCOUNT)
    with pytest.raises(AdapterError) as not_ready:
        await adapter.reconnect(ACCOUNT)
    assert not_ready.value.code in {"disconnected", "reauth_required"}
    await adapter.synthetic_authorize("synthetic:fixture")
    with pytest.raises(AdapterError) as healthy:
        await adapter.reconnect(ACCOUNT)
    assert healthy.value.code == "disconnected"
    with pytest.raises(AdapterError) as credential:
        await adapter.apply_authorization_update(
            {
                "@type": "updateAuthorizationState",
                "authorization_state": {"@type": "authorizationStateWaitPassword"},
            }
        )
    assert credential.value.code == "reauth_required"
    retry = await adapter.reconnect(ACCOUNT)
    assert retry.state == "wait_parameters"
    await adapter.synthetic_authorize("synthetic:fixture")
    await adapter.close()
    with pytest.raises(AdapterError):
        await adapter.status(ACCOUNT)
    assert AUTHORIZATION_UPDATE_MAP["authorizationStateReady"] == "ready"
