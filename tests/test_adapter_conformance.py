"""One behavioural contract, executed against every adapter implementation.

Adapters differ in capability (the synthetic simulator can mutate; the offline
TDLib boundary is read-only and cannot authorize without a synthetic fixture), but
they must be interchangeable wherever they claim a capability. This suite pins
the shared contract so a future native adapter can be validated the same way:

* normalized errors only — never upstream text, payloads or SDK types;
* explicit session state with a boolean intervention flag;
* account-scoped reads; unknown chats are ``None`` from ``get_chat`` and
  ``not_found`` from ``execute``;
* argument validation raises ``ValueError`` (programmer error) while operational
  rejection raises ``AdapterError``;
* ``close()`` is idempotent and refuses further service;
* update draining is bounded, account-scoped and typed.
"""

from dataclasses import dataclass
from typing import Any, Callable

import pytest

from tg_control.adapters.base import (
    ADAPTER_ERROR_CODES,
    AdapterError,
    SessionUnavailable,
    Update,
)
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.adapters.tdlib_boundary import (
    OfflineTdjsonTransport,
    OfflineTdlibAdapter,
)

ACCOUNT = "6b7c5d9e-1f2a-4b3c-8d5e-7f8a9b0c1d2e"
OTHER_ACCOUNT = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"
BOUNDARY_CHAT = {"id": 777, "chat_id": 777}
PUBLIC_CHAT = {
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


@dataclass(frozen=True)
class Profile:
    """One adapter implementation plus its declared capability matrix."""

    name: str
    ready: Callable[[], Any]
    unready: Callable[[], Any]
    read_only: bool
    supports_saved_messages: bool
    # The simulator provisions a fresh isolated fixture per account id; a real
    # adapter must reject ids it does not own.
    auto_provisions_accounts: bool


async def synthetic_ready():
    adapter = SyntheticAdapter()
    return adapter, ACCOUNT


async def synthetic_unready():
    adapter = SyntheticAdapter()
    adapter.set_state(ACCOUNT, "reauth_required")
    return adapter, ACCOUNT


async def boundary_ready():
    transport = OfflineTdjsonTransport()
    adapter = OfflineTdlibAdapter(transport, synthetic_account_id=ACCOUNT)
    await adapter.synthetic_authorize("synthetic:conformance")
    await adapter.register_chat(PUBLIC_CHAT, SUPERGROUP)
    return adapter, ACCOUNT


async def boundary_unready():
    transport = OfflineTdjsonTransport()
    adapter = OfflineTdlibAdapter(transport, synthetic_account_id=ACCOUNT)
    return adapter, ACCOUNT


PROFILES = {
    "synthetic": Profile(
        name="synthetic",
        ready=synthetic_ready,
        unready=synthetic_unready,
        read_only=False,
        supports_saved_messages=True,
        auto_provisions_accounts=True,
    ),
    "offline-tdlib": Profile(
        name="offline-tdlib",
        ready=boundary_ready,
        unready=boundary_unready,
        read_only=True,
        supports_saved_messages=False,
        auto_provisions_accounts=False,
    ),
}

PROFILE_IDS = sorted(PROFILES)


async def _ready(profile: Profile):
    adapter, account = await profile.ready()
    return adapter, account, await adapter.status(account)


def _assert_normalized(excinfo, *, allowed: set[str] | None = None) -> str:
    error = excinfo.value
    assert isinstance(error, AdapterError)
    assert error.code in ADAPTER_ERROR_CODES
    assert str(error) == error.code
    assert len(str(error)) <= 64
    if allowed is not None:
        assert error.code in allowed
    return error.code


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_adapter_exposes_the_protocol_surface(name):
    profile = PROFILES[name]
    adapter, _, status = await _ready(profile)
    for method in (
        "close",
        "status",
        "reconnect",
        "list_chats",
        "get_chat",
        "self_chat",
        "execute",
        "configure_media_retention",
        "prune_media",
        "drain_updates",
    ):
        assert callable(getattr(adapter, method, None)), method
    assert isinstance(adapter.persistent, bool)
    assert isinstance(status.state, str) and status.state
    assert isinstance(status.intervention_required, bool)


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_reads_are_account_scoped_and_unknown_chats_are_typed(name):
    profile = PROFILES[name]
    adapter, account, _ = await _ready(profile)
    chats = await adapter.list_chats(account)
    assert isinstance(chats, list)
    assert all(isinstance(chat.id, str) and chat.kind for chat in chats)
    assert await adapter.get_chat(account, "no-such-chat") is None
    with pytest.raises(AdapterError) as failure:
        await adapter.execute(account, "history", "no-such-chat", {})
    _assert_normalized(failure, allowed={"not_found"})
    if profile.auto_provisions_accounts:
        # The simulator owns an isolated fixture per account: state changes on
        # one account must never be visible on another.
        adapter.set_state(OTHER_ACCOUNT, "reauth_required")
        assert (await adapter.status(OTHER_ACCOUNT)).state == "reauth_required"
        assert (await adapter.status(account)).state == "ready"
    else:
        # A real adapter must reject account ids it does not own.
        with pytest.raises(AdapterError) as failure:
            await adapter.get_chat(OTHER_ACCOUNT, "public-channel")
        _assert_normalized(failure, allowed={"not_found"})


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_unknown_operations_are_normalized_and_read_only_adapters_refuse_writes(name):
    profile = PROFILES[name]
    adapter, account, _ = await _ready(profile)
    with pytest.raises(AdapterError) as failure:
        await adapter.execute(account, "not.a.real.operation", "chat", {})
    _assert_normalized(failure, allowed={"unsupported_operation", "not_found"})
    if profile.read_only:
        with pytest.raises(AdapterError) as failure:
            await adapter.execute(
                account, "telegram.send", "777", {"text": "synthetic conformance"}
            )
        _assert_normalized(failure, allowed={"unsupported_operation", "not_found"})


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_media_retention_arguments_are_validated(name):
    profile = PROFILES[name]
    adapter, account, _ = await _ready(profile)
    await adapter.configure_media_retention(None)
    await adapter.configure_media_retention(30)
    for invalid in (0, -1, 1.5, True, "30"):
        with pytest.raises(ValueError):
            await adapter.configure_media_retention(invalid)
    assert await adapter.prune_media(account) >= 0


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_update_draining_is_bounded_and_normalized(name):
    profile = PROFILES[name]
    adapter, account, _ = await _ready(profile)
    assert await adapter.drain_updates(account, 10) == []
    for invalid in (0, -1, 10**6, True):
        with pytest.raises(AdapterError) as failure:
            await adapter.drain_updates(account, invalid)
        _assert_normalized(failure, allowed={"unsupported_operation"})
    drained = await adapter.drain_updates(account, 1)
    assert isinstance(drained, list) and len(drained) <= 1
    assert all(isinstance(update, Update) for update in drained)


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_unready_sessions_are_explicit_and_fail_closed(name):
    profile = PROFILES[name]
    adapter, account = await profile.unready()
    status = await adapter.status(account)
    assert status.state in {
        "wait_parameters",
        "wait_phone_number",
        "wait_code",
        "wait_password",
        "reauth_required",
        "disconnected",
        "closed",
    }
    assert status.intervention_required is (status.state == "reauth_required")
    with pytest.raises(SessionUnavailable):
        await adapter.list_chats(account)


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_close_is_idempotent_and_refuses_further_service(name):
    profile = PROFILES[name]
    adapter, account, _ = await _ready(profile)
    await adapter.close()
    await adapter.close()  # idempotent
    with pytest.raises(SessionUnavailable):
        await adapter.status(account)
    with pytest.raises(SessionUnavailable):
        await adapter.list_chats(account)
    with pytest.raises(SessionUnavailable):
        await adapter.drain_updates(account, 1)


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_saved_messages_capability_is_declared_not_faked(name):
    profile = PROFILES[name]
    adapter, account, _ = await _ready(profile)
    if profile.supports_saved_messages:
        chat = await adapter.self_chat(account)
        assert chat.id
    else:
        with pytest.raises(AdapterError) as failure:
            await adapter.self_chat(account)
        _assert_normalized(failure, allowed={"unsupported_operation"})


@pytest.mark.parametrize("name", PROFILE_IDS)
async def test_errors_never_carry_upstream_text(name):
    profile = PROFILES[name]
    adapter, account, _ = await _ready(profile)
    with pytest.raises(AdapterError) as failure:
        await adapter.execute(account, "history", "secret-chat-name", {"limit": 5})
    code = _assert_normalized(failure)
    payload = f"{failure.value!r} {failure.value} {getattr(failure.value, 'args', '')}"
    assert "secret-chat-name" not in payload
    assert account not in payload
    assert code in ADAPTER_ERROR_CODES
