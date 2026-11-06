import asyncio
import base64
import os
import subprocess
import sys
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.fernet import Fernet, InvalidToken
from fastapi.testclient import TestClient
from test_api import auth, create_resources, execute, set_policy
from test_core import action, batch, setup

from tg_control.adapters.base import AdapterError
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.adapters.synthetic_store import SyntheticStore
from tg_control.api import create_app
from tg_control.cache import ReadCache
from tg_control.core import BatchRejected, Gateway
from tg_control.db import Grant


@pytest.mark.asyncio
async def test_encrypted_account_storage_retention_and_permanent_option(tmp_path):
    root = tmp_path / "state"
    key = Fernet.generate_key()
    now = [1_000.0]
    account_a, account_b = str(uuid.uuid4()), str(uuid.uuid4())
    adapter = SyntheticAdapter(SyntheticStore(root, key), clock=lambda: now[0])
    payload = base64.b64encode(b"synthetic secret bytes").decode()
    sent = await adapter.execute(
        account_a,
        "send_media",
        "public-channel",
        {
            "text": "synthetic secret caption",
            "media": {"filename": "test.txt", "content_type": "text/plain", "data_base64": payload},
        },
    )
    mid = sent["message_id"]
    data = (root / account_a / "state.enc").read_bytes()
    assert b"synthetic secret caption" not in data and payload.encode() not in data
    assert (root / account_a / "state.enc").stat().st_mode & 0o077 == 0
    assert adapter.persistent
    if os.name == "posix":
        with pytest.raises(RuntimeError, match="already in use"):
            SyntheticStore(root, key)
    await adapter.close()
    restarted = SyntheticAdapter(SyntheticStore(root, key), clock=lambda: now[0])
    assert (await restarted.execute(account_a, "get_media", "public-channel", {"message_id": mid}))[
        "media"
    ]["data_base64"] == payload
    assert all(
        m["text"] != "synthetic secret caption"
        for m in (await restarted.execute(account_b, "history", "public-channel", {}))["messages"]
    )
    await restarted.close()
    wrong = SyntheticAdapter(SyntheticStore(root, Fernet.generate_key()))
    with pytest.raises(InvalidToken):
        await wrong.status(account_a)
    await wrong.close()
    restarted = SyntheticAdapter(SyntheticStore(root, key), clock=lambda: now[0])
    await restarted.configure_media_retention(1)
    now[0] += 86401
    with pytest.raises(AdapterError) as error:
        await restarted.execute(account_a, "get_media", "public-channel", {"message_id": mid})
    assert error.value.code == "media_expired"
    history = await restarted.execute(account_a, "history", "public-channel", {})
    expired = next(m for m in history["messages"] if m["id"] == mid)
    assert expired["media"]["expired"] and "data_base64" not in expired["media"]
    # Expiry is persisted, and changing to permanent later cannot resurrect deleted bytes.
    await restarted.close()
    again = SyntheticAdapter(SyntheticStore(root, key), clock=lambda: now[0])
    await again.configure_media_retention(None)
    with pytest.raises(AdapterError, match="media_expired"):
        await again.execute(account_a, "get_media", "public-channel", {"message_id": mid})
    new = await again.execute(
        account_a,
        "send_media",
        "public-channel",
        {
            "text": "permanent synthetic",
            "media": {"filename": "keep.txt", "content_type": "text/plain", "data_base64": payload},
        },
    )
    now[0] += 400 * 86400
    assert (
        await again.execute(
            account_a, "get_media", "public-channel", {"message_id": new["message_id"]}
        )
    )["media"]["data_base64"] == payload
    await again.close()


def test_store_rejects_unsafe_account_paths_and_permissions(tmp_path):
    store = SyntheticStore(tmp_path / "state", Fernet.generate_key())
    with pytest.raises(ValueError):
        store.load("../outside")
    target = tmp_path / "other"
    target.mkdir()
    account = str(uuid.uuid4())
    (store.root / account).symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError):
        store.load(account)
    if os.name == "posix":
        insecure = tmp_path / "insecure"
        insecure.mkdir(mode=0o755)
        with pytest.raises(ValueError):
            SyntheticStore(insecure, Fernet.generate_key())
    store.close()


def test_cache_ttl_capacity_and_inflight_invalidation():
    clock = [0.0]
    cache = ReadCache(clock=lambda: clock[0], max_entries=1, max_bytes=1024)
    cache.configure(5)
    key = cache.key("a", "history", "c", {})
    gen = cache.generation("a")
    cache.put(key, {"messages": ["x"]}, gen)
    result = cache.get(key)
    result["messages"].append("tamper")
    assert cache.get(key) == {"messages": ["x"]}
    clock[0] = 5
    assert cache.get(key) is None
    cache.put(key, {"messages": ["x"]}, gen)
    cache.invalidate("a")
    cache.put(key, {"messages": ["stale"]}, gen)
    assert cache.get(key) is None
    gen = cache.generation("a")
    cache.put(key, {"messages": ["x" * 2000]}, gen)
    assert cache.get(key) is None
    cache.configure(0)
    cache.put(key, {"messages": ["x"]}, cache.generation("a"))
    assert cache.get(key) is None


@pytest.mark.asyncio
async def test_cache_rechecks_policy_and_invalidates_on_mutation(database):
    account, _, client_id, principal = setup(database)

    class CountingAdapter(SyntheticAdapter):
        reads = 0

        async def execute(self, account_id, operation, chat_id, params):
            if operation == "history":
                self.reads += 1
            return await super().execute(account_id, operation, chat_id, params)

    adapter = CountingAdapter()
    gateway = Gateway(database, adapter)
    await gateway.configure_retention(60, 30)
    request = batch(action(account, "history", "public-channel"))
    await gateway.execute(principal, request)
    await gateway.execute(principal, request)
    assert adapter.reads == 1
    await gateway.execute(
        principal,
        batch(action(account, "send", "public-channel", text="new synthetic"), key=uuid.uuid4()),
    )
    result = await gateway.execute(principal, request)
    assert adapter.reads == 2
    assert any(m["text"] == "new synthetic" for m in result["items"][0]["data"]["messages"])
    with database() as session:
        grant = session.get(Grant, (client_id, account))
        grant.read_scope = {**grant.read_scope, "exclude_chats": ["public-channel"]}
        session.commit()
    with pytest.raises(BatchRejected):
        await gateway.execute(principal, request)
    assert adapter.reads == 2
    await gateway.configure_retention(0, 30)
    assert not gateway._cache._entries


@pytest.mark.asyncio
async def test_inflight_read_cannot_repopulate_cache_after_write(database):
    account, _, _, principal = setup(database)
    read_started, release = asyncio.Event(), asyncio.Event()

    class SlowReadAdapter(SyntheticAdapter):
        delay = True

        async def execute(self, account_id, operation, chat_id, params):
            result = await super().execute(account_id, operation, chat_id, params)
            if operation == "history" and self.delay:
                self.delay = False
                read_started.set()
                await release.wait()
            return result

    gateway = Gateway(database, SlowReadAdapter())
    await gateway.configure_retention(60, 30)
    request = batch(action(account, "history", "public-channel"))
    pending = asyncio.create_task(gateway.execute(principal, request))
    await read_started.wait()
    await gateway.execute(
        principal,
        batch(action(account, "send", "public-channel", text="after read"), key=uuid.uuid4()),
    )
    release.set()
    await pending
    result = await gateway.execute(principal, request)
    assert any(m["text"] == "after read" for m in result["items"][0]["data"]["messages"])


def test_http_permanent_media_survives_restart(settings, tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "TG_DATABASE_URL": settings.database_url}
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env=env,
        check=True,
        capture_output=True,
    )
    settings = replace(settings, synthetic_state_dir=tmp_path / "synthetic-state")
    with TestClient(create_app(settings)) as client:
        owner, account, actor = create_resources(client, settings)
        set_policy(client, owner, account, actor)
        assert (
            client.put(
                "/v1/admin/settings",
                headers=owner,
                json={
                    "audit_retention_days": 90,
                    "cache_ttl_seconds": 0,
                    "media_retention_days": None,
                },
            ).status_code
            == 200
        )
        media = {
            "filename": "synthetic.txt",
            "content_type": "text/plain",
            "data_base64": "c2FmZQ==",
        }
        result = execute(
            client,
            actor["credential"],
            account,
            "send_media",
            "public-channel",
            key=uuid.uuid4(),
            text="synthetic",
            media=media,
        )
        mid = result.json()["items"][0]["data"]["message_id"]
    with TestClient(create_app(settings)) as client:
        result = execute(
            client, actor["credential"], account, "get_media", "public-channel", message_id=mid
        )
        assert result.json()["items"][0]["data"]["media"]["data_base64"] == "c2FmZQ=="
        assert (
            client.get("/v1/admin/settings", headers=auth(settings.owner_token)).json()[
                "media_retention_days"
            ]
            is None
        )


@pytest.mark.asyncio
async def test_failed_encrypted_write_does_not_mutate_in_memory(tmp_path, monkeypatch):
    store = SyntheticStore(tmp_path / "state", Fernet.generate_key())
    adapter = SyntheticAdapter(store)
    account = str(uuid.uuid4())
    await adapter.status(account)  # initialize source state successfully

    def fail(*_args):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(store, "save", fail)
    with pytest.raises(OSError):
        await adapter.execute(account, "send", "public-channel", {"text": "not committed"})
    history = await adapter.execute(account, "history", "public-channel", {})
    assert all(message["text"] != "not committed" for message in history["messages"])
    await adapter.close()


@pytest.mark.asyncio
async def test_media_maintenance_includes_disabled_accounts_and_audits(database):
    from sqlalchemy import select

    from tg_control.db import Account, AuditEvent

    account, disabled, _, _ = setup(database)
    now = [10_000.0]
    adapter = SyntheticAdapter(clock=lambda: now[0])
    gateway = Gateway(database, adapter)
    await adapter.status(account)
    await adapter.status(disabled)
    with database() as session:
        session.get(Account, disabled).enabled = False
        session.commit()
    await gateway.configure_retention(0, 1)
    now[0] += 86401
    await gateway.maintain_media()
    with database() as session:
        events = session.scalars(
            select(AuditEvent).where(AuditEvent.event == "media_retention")
        ).all()
        assert {event.account_id for event in events} == {account, disabled}
        assert all(event.actor_kind == "system" and event.decision == "success" for event in events)
    for identifier in [account, disabled]:
        with pytest.raises(AdapterError, match="media_expired"):
            await adapter.execute(identifier, "get_media", "public-channel", {"message_id": 2})
