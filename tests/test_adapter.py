import uuid
from pathlib import Path

import pytest

from tg_control.adapters.base import AdapterError, RateLimited, SessionUnavailable
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.adapters.tdlib_poc import OfflineTdJsonRouter, isolated_paths


@pytest.mark.asyncio
async def test_accounts_are_isolated_and_writes_remain_local():
    adapter = SyntheticAdapter()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    result = await adapter.execute(a, "send", "public-channel", {"text": "only account a"})
    assert result["message_id"] == 10
    history_a = await adapter.execute(a, "history", "public-channel", {})
    history_b = await adapter.execute(b, "history", "public-channel", {})
    assert any(m["text"] == "only account a" for m in history_a["messages"])
    assert all(m["text"] != "only account a" for m in history_b["messages"])
    assert history_a["history_complete"] is False


@pytest.mark.asyncio
async def test_lifecycle_and_faults_are_account_scoped():
    adapter = SyntheticAdapter()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    adapter.set_state(a, "disconnected")
    with pytest.raises(SessionUnavailable) as exc:
        await adapter.list_chats(a)
    assert not exc.value.intervention_required
    assert await adapter.list_chats(b)
    assert (await adapter.reconnect(a)).state == "ready"
    adapter.set_state(a, "reauth_required")
    with pytest.raises(SessionUnavailable) as exc:
        await adapter.reconnect(a)
    assert exc.value.intervention_required
    adapter.inject_fault(b, "send", RateLimited(2.5))
    with pytest.raises(RateLimited) as exc:
        await adapter.execute(b, "send", "public-channel", {"text": "test"})
    assert exc.value.retry_after == 2.5
    assert (await adapter.execute(b, "send", "public-channel", {"text": "test"}))["message_id"]


@pytest.mark.asyncio
async def test_media_and_missing_message():
    adapter = SyntheticAdapter()
    a = str(uuid.uuid4())
    media = await adapter.execute(a, "get_media", "public-channel", {"message_id": 2})
    assert media["media"]["filename"] == "sample.txt"
    listing = await adapter.execute(a, "history", "public-channel", {})
    assert "data_base64" not in listing["messages"][0]["media"]
    with pytest.raises(AdapterError, match="not_found"):
        await adapter.execute(a, "get_message", "public-channel", {"message_id": 999})


def test_offline_tdjson_correlates_clients_and_updates():
    router = OfflineTdJsonRouter()
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    router.register(1, a)
    router.register(2, b)
    req = router.request(1, "getChatHistory")
    # A response attributed to another native client cannot complete this request.
    assert router.receive(2, {"@type": "messages", "@extra": req["@extra"]}) is None
    assert router.receive(1, {"@type": "messages", "@extra": req["@extra"]}) == (
        a,
        "getChatHistory",
    )
    router.receive(
        2,
        {
            "@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationStateWaitCode"},
        },
    )
    assert router.states[b] == "reauth_required"
    assert router.states[a] == "initializing"
    with pytest.raises(ValueError):
        router.register(3, a)


def test_tdlib_paths_are_isolated_and_reject_traversal():
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    assert isolated_paths(Path("/state"), a).database != isolated_paths(Path("/state"), b).database
    with pytest.raises(ValueError):
        isolated_paths(Path("/state"), "../../escape")


def test_invalid_retry_duration_is_rejected():
    for duration in (-1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            RateLimited(duration)
