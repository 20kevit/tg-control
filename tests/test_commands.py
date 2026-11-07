"""Reply-aware /save acceptance tests through Core, Policy, Media and Audit."""

import asyncio
import base64
import uuid

import pytest
from sqlalchemy import select, update

from tg_control.adapters.base import AdapterError, ChatKind, RateLimited
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.auth import Principal, rotate_credential
from tg_control.commands import CommandError, CommandRouter, SaveRequest
from tg_control.core import Gateway
from tg_control.db import Account, AuditEvent, Client, Credential, Grant
from tg_control.media import MediaPipeline
from tg_control.policy import Scope


class Env:
    def __init__(self, database, tmp_path):
        self.database = database
        self.adapter = SyntheticAdapter()
        self.gateway = Gateway(database, self.adapter, sleep=lambda _delay: asyncio.sleep(0))
        self.media = MediaPipeline(tmp_path / "staging")
        self.router = CommandRouter(self.gateway, self.adapter, self.media, database)
        with database() as session:
            self.account = Account(alias="command account", phone_encrypted="synthetic")
            self.other_account = Account(alias="other account", phone_encrypted="synthetic")
            self.client = Client(name="command client")
            session.add_all([self.account, self.other_account, self.client])
            session.commit()
            rotate_credential(session, self.client.id)
            session.commit()
            self.principal = Principal(
                client_id=self.client.id,
                credential_id=session.scalar(
                    select(Credential.id).where(
                        Credential.client_id == self.client.id,
                        Credential.revoked.is_(False),
                    )
                ),
            )
        self.grant()

    def grant(self, *, read=True, write=True, source_types=None, destination=True):
        read_scope = (
            Scope(
                enabled=True,
                allow_types=source_types or [ChatKind.PUBLIC_CHANNEL],
                allow_chats=["saved-messages"] if destination else [],
            )
            if read
            else Scope()
        )
        write_chats = ["saved-messages"] if destination else ["public-channel"]
        write_scope = Scope(enabled=True, allow_chats=write_chats) if write else Scope()
        with self.database() as session:
            existing = session.get(Grant, (self.client.id, self.account.id))
            values = {
                "read_scope": read_scope.model_dump(mode="json"),
                "write_scope": write_scope.model_dump(mode="json"),
                "admin_scope": Scope().model_dump(mode="json"),
            }
            if existing is None:
                session.add(
                    Grant(client_id=self.client.id, account_id=self.account.id, **values)
                )
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            session.commit()

    def request(self, message_id=1, chat_id="public-channel", key=None):
        return SaveRequest(
            account_id=self.account.id,
            chat_id=chat_id,
            message_id=message_id,
            idempotency_key=key or uuid.uuid4(),
        )

    async def saved_messages(self, account_id=None):
        data = await self.adapter.execute(
            str(account_id or self.account.id), "history", "saved-messages", {"limit": 50}
        )
        return data["messages"]


@pytest.fixture
def env(database, tmp_path):
    return Env(database, tmp_path)


async def test_save_text_message_preserves_content_and_audits_metadata_only(env):
    result = await env.router.invoke(env.principal, "save", env.request())
    assert result["status"] == "success"
    assert result["destination_chat_id"] == "saved-messages"
    assert result["has_media"] is False
    saved = await env.saved_messages()
    assert saved[0]["text"] == f"Synthetic fixture {env.account.id} in public-channel"
    with env.database() as session:
        rows = session.scalars(select(AuditEvent).where(AuditEvent.event == "command_save")).all()
    assert rows and all(row.decision == "success" for row in rows)
    serialized = " ".join(
        f"{row.event} {row.decision} {row.error_code} {row.chat_id} {row.account_id}"
        for row in rows
    )
    assert "Synthetic fixture" not in serialized


async def test_save_media_message_preserves_filename_and_caption(env, monkeypatch):
    env.grant(destination=True)
    result = await env.router.invoke(env.principal, "save", env.request(message_id=2))
    assert result["status"] == "success" and result["has_media"] is True
    saved = await env.saved_messages()
    assert saved[0]["text"] == "Synthetic attachment"
    assert saved[0]["media"]["filename"] == "sample.txt"
    assert saved[0]["media"]["content_type"] == "text/plain"
    fetched = await env.adapter.execute(
        str(env.account.id), "get_media", "saved-messages", {"message_id": saved[0]["id"]}
    )
    assert base64.b64decode(fetched["media"]["data_base64"]) == b"synthetic file\n"


async def test_save_requires_read_on_source_and_write_on_saved_messages(env):
    env.grant(read=False, destination=True)
    with pytest.raises(CommandError) as denied:
        await env.router.invoke(env.principal, "save", env.request())
    assert denied.value.code == "denied" and denied.value.status_code == 403
    env.grant(destination=False)
    with pytest.raises(CommandError) as destination:
        await env.router.invoke(env.principal, "save", env.request())
    assert destination.value.code == "destination_denied"
    expected = f"Synthetic fixture {env.account.id} in public-channel"
    assert all(message["text"] != expected for message in await env.saved_messages())


async def test_save_resolves_reply_in_selected_account_only(env):
    env.grant(destination=True)
    with pytest.raises(CommandError) as missing:
        await env.router.invoke(env.principal, "save", env.request(message_id=999))
    assert missing.value.code == "unresolved_reply" and missing.value.status_code == 404
    with pytest.raises(CommandError) as wrong_chat:
        await env.router.invoke(env.principal, "save", env.request(chat_id="private-direct"))
    assert wrong_chat.value.code == "denied"
    # Another account's Saved Messages never receive this content.
    assert all(
        message["text"] != f"Synthetic fixture {env.account.id} in public-channel"
        for message in await env.saved_messages(env.other_account.id)
    )


async def test_save_duplicate_invocation_is_rejected(env):
    key = uuid.uuid4()
    first = await env.router.invoke(env.principal, "save", env.request(key=key))
    assert first["status"] == "success"
    with pytest.raises(CommandError) as duplicate:
        await env.router.invoke(env.principal, "save", env.request(key=key))
    assert duplicate.value.code == "duplicate_invocation" and duplicate.value.status_code == 409
    expected = f"Synthetic fixture {env.account.id} in public-channel"
    saved = [m for m in await env.saved_messages() if m["text"] == expected]
    assert len(saved) == 1


async def test_save_unsupported_and_unavailable_content(env):
    env.grant(destination=True)
    await env.adapter.execute(
        str(env.account.id), "edit", "public-channel", {"message_id": 1, "text": ""}
    )
    with pytest.raises(CommandError) as unsupported:
        await env.router.invoke(env.principal, "save", env.request())
    assert unsupported.value.code == "unsupported_content"

    env.adapter.inject_fault(str(env.account.id), "get_media", AdapterError("media_expired"))
    with pytest.raises(CommandError) as unavailable:
        await env.router.invoke(env.principal, "save", env.request(message_id=2))
    assert unavailable.value.code == "unavailable_source"


async def test_save_rate_limit_and_unknown_outcome_reporting(env):
    env.grant(destination=True)
    # A wait longer than the remaining deadline is reported, not silently dropped.
    env.adapter.inject_fault(str(env.account.id), "send", RateLimited(40))
    with pytest.raises(CommandError) as limited:
        await env.router.invoke(env.principal, "save", env.request())
    assert limited.value.code == "rate_limit"
    assert limited.value.retry_after == 40
    assert limited.value.outcome_unknown is False
    expected = f"Synthetic fixture {env.account.id} in public-channel"
    assert all(message["text"] != expected for message in await env.saved_messages())
    env.adapter.inject_fault(
        str(env.account.id), "send", AdapterError("adapter_failure", outcome_unknown=True)
    )
    with pytest.raises(CommandError) as unknown:
        await env.router.invoke(env.principal, "save", env.request())
    assert unknown.value.code == "outcome_unknown"
    assert unknown.value.outcome_unknown is True


async def test_save_rejects_revoked_credentials_and_cleans_staged_media(env):
    env.grant(destination=True)
    with env.database() as session:
        session.execute(
            update(Credential)
            .where(Credential.id == env.principal.credential_id)
            .values(revoked=True)
        )
        session.commit()
    with pytest.raises(CommandError) as revoked:
        await env.router.invoke(env.principal, "save", env.request())
    assert revoked.value.code == "credential_revoked"
    assert env.media.total_usage() == 0


async def test_save_media_staging_failure_leaves_no_files(env, monkeypatch):
    env.grant(destination=True)
    async def failing_stage(*_args, **_kwargs):
        raise __import__("tg_control.media", fromlist=["MediaError"]).MediaError(
            "media_too_large", 413
        )

    monkeypatch.setattr(env.media, "stage", failing_stage)
    with pytest.raises(CommandError) as too_large:
        await env.router.invoke(env.principal, "save", env.request(message_id=2))
    assert too_large.value.code == "media_too_large"
    assert env.media.total_usage() == 0


def test_command_catalog_is_explicit(env):
    catalog = env.router.catalog()
    assert [entry["name"] for entry in catalog] == ["save"]
    assert catalog[0]["reply_required"] is True
    assert "write:saved_messages" in catalog[0]["required_permissions"]


async def test_unknown_command_is_rejected(env):
    with pytest.raises(CommandError) as unknown:
        await env.router.invoke(env.principal, "drop_tables", env.request())
    assert unknown.value.code == "unknown_command" and unknown.value.status_code == 404
