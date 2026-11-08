"""Feature-expansion contracts and synthetic-only acceptance tests.

No test in this module logs into Telegram or uses non-synthetic content.
"""

import ast
import asyncio
import base64
import inspect
import textwrap
import uuid

import pytest
from pydantic import ValidationError

from tg_control.adapters.base import AdapterError, ChatKind
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.auth import authenticate, rotate_credential
from tg_control.capabilities import capability_catalog
from tg_control.core import BatchRejected, Gateway
from tg_control.db import Account, Client, Grant
from tg_control.features import (
    FEATURE_ACTION_MODELS,
    FEATURE_SPECS,
    SYNTHETIC_FEATURE_OPERATIONS,
)
from tg_control.policy import Scope
from tg_control.schemas import Batch


def setup(database, *, read=None, write=None, admin=None):
    with database() as session:
        suffix = uuid.uuid4().hex[:8]
        account = Account(alias=f"synthetic features {suffix}", phone_encrypted="synthetic")
        client = Client(name=f"feature client {suffix}")
        session.add_all([account, client])
        session.flush()
        grant = Grant(
            client_id=client.id,
            account_id=account.id,
            read_scope=(read or Scope()).model_dump(mode="json"),
            write_scope=(write or Scope()).model_dump(mode="json"),
            admin_scope=(admin or Scope()).model_dump(mode="json"),
        )
        session.add(grant)
        token = rotate_credential(session, client.id)
        session.commit()
        return account.id, client.id, authenticate(session, token)


def feature_action(account_id, operation, chat_id=None, **params):
    action = {
        "account_id": account_id,
        "operation": operation,
        "params": params,
    }
    if chat_id is not None:
        action["chat_id"] = chat_id
    return action


def make_batch(*actions, key=None):
    return Batch.model_validate(
        {"actions": list(actions), "idempotency_key": str(key) if key else None}
    )


async def adapter_feature(adapter, account_id, operation, chat_id=None, **params):
    return await adapter.execute(
        account_id,
        operation,
        chat_id or "__account__",
        {"params": params},
    )


def test_feature_models_are_strict_and_mutations_require_batch_idempotency():
    account_id = str(uuid.uuid4())
    valid = feature_action(account_id, "get_chat_info", "public-group")
    batch = Batch.model_validate({"actions": [valid]})
    assert batch.actions[0].operation == "get_chat_info"

    with pytest.raises(ValidationError):
        Batch.model_validate(
            {"actions": [feature_action(account_id, "set_draft", "public-group", text="draft")]}
        )
    with pytest.raises(ValidationError):
        Batch.model_validate(
            {
                "actions": [
                    feature_action(
                        account_id, "create_poll", "public-group", question="Q", options=["A", "a"]
                    )
                ],
                "idempotency_key": str(uuid.uuid4()),
            }
        )
    with pytest.raises(ValidationError):
        Batch.model_validate(
            {
                "actions": [
                    feature_action(
                        account_id,
                        "schedule_message",
                        "public-group",
                        text="synthetic only",
                        run_at="2026-11-09T12:00:00",
                    )
                ],
                "idempotency_key": str(uuid.uuid4()),
            }
        )
    with pytest.raises(ValidationError):
        Batch.model_validate(
            {
                "actions": [
                    {
                        **valid,
                        "params": {"unexpected": "field"},
                    }
                ]
            }
        )


def test_synthetic_allowlist_has_a_handler_branch_for_every_operation():
    source = textwrap.dedent(inspect.getsource(SyntheticAdapter._execute_feature))
    constants = {
        node.value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert SYNTHETIC_FEATURE_OPERATIONS <= constants
    assert not (SYNTHETIC_FEATURE_OPERATIONS - set(FEATURE_SPECS))


def test_feature_inventory_has_complete_registered_schemas_and_capabilities():
    assert len(FEATURE_SPECS) == 135
    assert len(FEATURE_ACTION_MODELS) == len(FEATURE_SPECS)
    assert set(FEATURE_ACTION_MODELS) == set(FEATURE_SPECS)
    assert len(SYNTHETIC_FEATURE_OPERATIONS) == 124
    assert set(FEATURE_SPECS) - SYNTHETIC_FEATURE_OPERATIONS == {
        "get_fact_check",
        "translate_message",
        "get_folder_suggestions",
        "set_story_stealth",
        "get_outbox_read_date",
        "get_read_participants",
        "convert_gift",
        "manage_star_subscription",
        "transcribe_audio",
        "edit_fact_check",
        "delete_fact_check",
    }
    catalog = capability_catalog("inventory")
    assert len(catalog["capabilities"]) == 12 + len(FEATURE_SPECS) == 147
    rows = {item["operation"]: item for item in catalog["capabilities"]}
    assert set(rows) == set(FEATURE_SPECS) | {
        "list_chats",
        "search",
        "history",
        "get_message",
        "get_media",
        "send",
        "send_media",
        "edit",
        "delete",
        "forward",
        "set_title",
        "set_member_status",
    }
    for operation in FEATURE_SPECS:
        assert rows[operation]["adapter_support"] == (
            "synthetic_fixture"
            if operation in SYNTHETIC_FEATURE_OPERATIONS
            else "not_implemented_or_version_unverified"
        )
    assert all(row["live_status"] == "blocked_compliance" for row in rows.values())
    assert catalog["live_adapter_enabled"] is False


@pytest.mark.asyncio
async def test_synthetic_message_interactions_and_media_are_isolated(database):
    account_id = str(uuid.uuid4())
    adapter = SyntheticAdapter()

    await adapter_feature(adapter, account_id, "set_draft", "public-group", text="synthetic draft")
    assert (await adapter_feature(adapter, account_id, "get_draft", "public-group"))["draft"][
        "text"
    ] == "synthetic draft"

    album = await adapter_feature(
        adapter,
        account_id,
        "send_album",
        "public-group",
        caption="fixture album",
        items=[
            {
                "filename": f"image-{n}.png",
                "content_type": "image/png",
                "data_base64": base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode(),
            }
            for n in range(2)
        ],
    )
    assert len(album["items"]) == 2 and album["atomic"] is False

    voice = await adapter_feature(
        adapter,
        account_id,
        "send_voice",
        "public-group",
        filename="voice.ogg",
        content_type="audio/ogg",
        data_base64=base64.b64encode(b"OggSsynthetic").decode(),
    )
    assert voice["media_kind"] == "voice"
    with pytest.raises(AdapterError) as invalid_media:
        await adapter_feature(
            adapter,
            account_id,
            "send_video_note",
            "public-group",
            filename="spoof.mp4",
            content_type="video/mp4",
            data_base64=base64.b64encode(b"not an mp4").decode(),
        )
    assert getattr(invalid_media.value, "code", None) == "unsupported_operation"

    adapter._account(account_id).features["eligibility"]["premium"] = True
    with pytest.raises(AdapterError) as paid_reaction:
        await adapter_feature(
            adapter,
            account_id,
            "set_reaction",
            "public-group",
            message_id=1,
            reaction="⭐",
            paid=True,
        )
    assert getattr(paid_reaction.value, "code", None) == "approval_required"
    assert (
        await adapter_feature(adapter, account_id, "get_reactions", "public-group", message_id=1)
    )["reactions"] == []

    poll = await adapter_feature(
        adapter,
        account_id,
        "create_poll",
        "public-group",
        question="Synthetic question?",
        options=["Synthetic A", "Synthetic B"],
    )
    result = await adapter_feature(
        adapter,
        account_id,
        "vote_poll",
        "public-group",
        message_id=poll["message_id"],
        option_indexes=[1],
    )
    assert result["voted"] is True
    assert (
        await adapter_feature(
            adapter, account_id, "get_poll", "public-group", message_id=poll["message_id"]
        )
    )["poll"]["options"][1]["voters"] == 1

    updates = await adapter.drain_updates(account_id, limit=200)
    assert {item.event_type for item in updates} >= {"message.new", "poll.updated"}
    assert all("data_base64" not in str(item.payload) for item in updates)
    await adapter.close()


@pytest.mark.asyncio
async def test_synthetic_groups_contacts_folders_profile_and_business_are_typed(database):
    account_id = str(uuid.uuid4())
    adapter = SyntheticAdapter()
    account = adapter._account(account_id)
    account.features["eligibility"].update(
        {"business": True, "stars": True, "boosts": True, "group_calls": True}
    )

    created = await adapter_feature(
        adapter,
        account_id,
        "save_chat_folder",
        folder_id=None,
        title="Synthetic folder",
        chat_ids=["public-group"],
        excluded_chat_ids=[],
    )
    folder_id = created["folder"]["folder_id"]
    assert (await adapter_feature(adapter, account_id, "list_chat_folders"))["folders"][0][
        "folder_id"
    ] == folder_id
    await adapter_feature(adapter, account_id, "reorder_chat_folders", folder_ids=[folder_id])

    profile_batch = make_batch(
        feature_action(
            account_id,
            "update_profile_details",
            birthday="1990-01-02",
            profile_color=0x123456,
        ),
        key=uuid.uuid4(),
    )
    profile_action = profile_batch.actions[0]
    await adapter.execute(
        account_id,
        profile_action.operation,
        "__account__",
        {"params": profile_action.params.model_dump()},
    )
    profile = (await adapter_feature(adapter, account_id, "get_profile"))["profile"]
    assert profile["birthday"] == "1990-01-02" and profile["profile_color"] == 0x123456

    link = await adapter_feature(
        adapter,
        account_id,
        "create_business_link",
        title="Synthetic link",
        message="Fixture only",
        button_text="Open",
        url="https://example.invalid/",
    )
    slug = link["link"]["slug"]
    assert (await adapter_feature(adapter, account_id, "resolve_business_link", slug=slug))["link"][
        "link_id"
    ] == link["link"]["link_id"]
    await adapter_feature(
        adapter,
        account_id,
        "update_business_link",
        link_id=link["link"]["link_id"],
        title="Updated link",
        message="Updated",
        button_text="View",
        url=None,
    )
    assert (await adapter_feature(adapter, account_id, "get_stars"))["currency"] == "XTR"
    assert (await adapter_feature(adapter, account_id, "get_group_call", "public-group"))[
        "active"
    ] is False

    with pytest.raises(AdapterError) as blocked:
        await adapter_feature(adapter, account_id, "convert_gift", item_id="synthetic-gift-1")
    assert getattr(blocked.value, "code", None) == "approval_required"
    with pytest.raises(AdapterError) as blocked_subscription:
        await adapter_feature(adapter, account_id, "manage_star_subscription")
    assert getattr(blocked_subscription.value, "code", None) == "approval_required"
    await adapter.close()


@pytest.mark.asyncio
async def test_translation_remains_compliance_blocked(database):
    account_id, _, _ = setup(database)
    adapter = SyntheticAdapter()
    with pytest.raises(AdapterError) as blocked:
        await adapter_feature(
            adapter, account_id, "translate_message", "public-channel", message_id=1
        )
    assert getattr(blocked.value, "code", None) == "compliance_blocked"
    await adapter.close()


@pytest.mark.asyncio
async def test_core_keeps_read_write_admin_independent_and_preauthorizes_features(database):
    account_id, _, principal = setup(
        database,
        read=Scope(enabled=True, allow_types=[ChatKind.PUBLIC_GROUP]),
        write=Scope(enabled=True, allow_chats=["public-channel"]),
        admin=Scope(enabled=True, allow_chats=["private-group"]),
    )
    gateway = Gateway(database, SyntheticAdapter())

    read = await gateway.execute(
        principal,
        make_batch(feature_action(account_id, "get_chat_info", "public-group")),
    )
    assert read["items"][0]["status"] == "success"

    admin_read = await gateway.execute(
        principal,
        make_batch(feature_action(account_id, "get_admin_log", "private-group", limit=10)),
    )
    assert admin_read["items"][0]["status"] == "success"

    with pytest.raises(BatchRejected):
        await gateway.execute(
            principal,
            make_batch(feature_action(account_id, "get_draft", "public-channel")),
        )

    key = uuid.uuid4()
    with pytest.raises(BatchRejected) as denied:
        await gateway.execute(
            principal,
            make_batch(
                feature_action(account_id, "set_draft", "public-channel", text="must not persist"),
                feature_action(account_id, "set_draft", "private-group", text="not allowed"),
                key=key,
            ),
        )
    assert denied.value.status_code == 403
    adapter = gateway.adapter
    assert (await adapter.execute(account_id, "get_draft", "public-channel", {"params": {}}))[
        "draft"
    ] is None

    excluded_account, _, excluded_principal = setup(
        database,
        read=Scope(
            enabled=True,
            allow_types=[ChatKind.PUBLIC_GROUP],
            exclude_chats=["public-group"],
        ),
    )
    with pytest.raises(BatchRejected):
        await gateway.execute(
            excluded_principal,
            make_batch(feature_action(excluded_account, "get_chat_info", "public-group")),
        )


@pytest.mark.asyncio
async def test_paid_reactions_are_blocked_even_when_fixture_is_eligible(database):
    account_id, _, principal = setup(
        database,
        read=Scope(enabled=True, allow_chats=["public-group"]),
        write=Scope(enabled=True, allow_chats=["public-group"]),
    )
    adapter = SyntheticAdapter()
    adapter._account(account_id).features["eligibility"]["premium"] = True
    gateway = Gateway(database, adapter)

    result = await gateway.execute(
        principal,
        make_batch(
            feature_action(
                account_id,
                "set_reaction",
                "public-group",
                message_id=1,
                reaction="⭐",
                paid=True,
            ),
            key=uuid.uuid4(),
        ),
    )
    assert result["items"][0]["error"] == "approval_required"
    reactions = await adapter_feature(
        adapter, account_id, "get_reactions", "public-group", message_id=1
    )
    assert reactions["reactions"] == []


async def test_account_level_feature_outputs_respect_chat_scopes(database):
    account_id, _, principal = setup(
        database,
        read=Scope(enabled=True, allow_types=[ChatKind.PUBLIC_CHANNEL]),
        write=Scope(enabled=True, allow_chats=["public-channel"]),
    )
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter)
    account = adapter._account(account_id)
    account.features["chat_folders"] = [
        {
            "folder_id": "mixed-folder",
            "title": "Synthetic folder",
            "chat_ids": ["public-channel", "private-group"],
            "excluded_chat_ids": [],
        }
    ]
    account.features["profile"]["personal_channel_id"] = "private-group"

    profile = await gateway.execute(
        principal,
        make_batch(feature_action(account_id, "get_profile")),
    )
    assert profile["items"][0]["data"]["profile"]["personal_channel_id"] is None

    with pytest.raises(BatchRejected):
        await gateway.execute(
            principal,
            make_batch(
                feature_action(
                    account_id,
                    "update_profile_details",
                    birthday="1990-01-02",
                    personal_channel_id="private-group",
                ),
                key=uuid.uuid4(),
            ),
        )
    assert account.features["profile"]["birthday"] is None

    result = await gateway.execute(
        principal,
        make_batch(feature_action(account_id, "list_chat_folders")),
    )
    assert result["items"][0]["data"]["folders"][0]["chat_ids"] == ["public-channel"]
    assert "private-group" not in str(result)

    discussion = await gateway.execute(
        principal,
        make_batch(feature_action(account_id, "get_discussion", "public-channel", message_id=2)),
    )
    assert discussion["items"][0]["data"] == {"chat_id": None, "available": False}

    with pytest.raises(BatchRejected):
        await gateway.execute(
            principal,
            make_batch(
                feature_action(
                    account_id,
                    "save_chat_folder",
                    folder_id=None,
                    title="cross-scope",
                    chat_ids=["public-channel", "private-group"],
                    excluded_chat_ids=[],
                ),
                key=uuid.uuid4(),
            ),
        )
    assert len(account.features["chat_folders"]) == 1


async def test_capability_discovery_is_account_scoped_and_live_remains_blocked(database):
    account_id, _, principal = setup(
        database,
        read=Scope(enabled=True, allow_types=[ChatKind.PUBLIC_CHANNEL]),
    )
    adapter = SyntheticAdapter()
    adapter.set_state(account_id, "reauth_required")
    gateway = Gateway(database, adapter)
    result = await gateway.capabilities(principal, account_id)
    assert result["adapter_mode"] == "synthetic"
    assert result["live_adapter_enabled"] is False
    operations = {item["operation"]: item for item in result["capabilities"]}
    assert all(item["permission"] == "read" for item in result["capabilities"])
    assert operations["get_chat_info"]["adapter_support"] == "synthetic_fixture"
    assert operations["list_chats"]["status"] == "synthetic_fixture_supported"
    assert operations["get_business_settings"]["status"] == "capability_unavailable"
    assert operations["get_business_settings"]["adapter_support"] == "synthetic_fixture"
    assert operations["get_folder_suggestions"]["status"] == "not_implemented_or_unverified"
    assert operations["translate_message"]["status"] == "compliance_blocked"
    assert "send" not in operations

    full_catalog = capability_catalog(account_id)
    full_operations = {item["operation"]: item for item in full_catalog["capabilities"]}
    assert full_operations["convert_gift"]["status"] == "approval_required"
    assert full_operations["manage_star_subscription"]["status"] == "approval_required"
    assert full_operations["set_reaction"]["conditional_blockers"] == {"paid": "approval_required"}
    assert "approval_required" in full_operations["set_reaction"]["normalized_errors"]

    unassigned, _, unassigned_principal = setup(database)
    with pytest.raises(BatchRejected) as denied:
        await gateway.capabilities(unassigned_principal, unassigned)
    assert denied.value.status_code == 403

    catalog = capability_catalog(account_id)
    assert catalog["live_adapter_enabled"] is False
    assert all(item["live_status"] == "blocked_compliance" for item in catalog["capabilities"])


@pytest.mark.asyncio
async def test_capability_discovery_filters_permission_and_chat_scopes(database):
    account_id, _, principal = setup(
        database,
        read=Scope(enabled=True, allow_types=[ChatKind.PUBLIC_CHANNEL]),
        write=Scope(enabled=True, allow_chats=["private-group"]),
        admin=Scope(enabled=True, allow_chats=["private-direct"]),
    )
    result = await Gateway(database, SyntheticAdapter()).capabilities(principal, account_id)
    operations = {item["operation"]: item for item in result["capabilities"]}

    assert operations["get_chat_info"]["permission"] == "read"
    assert operations["send"]["permission"] == "write"
    assert operations["set_title"]["permission"] == "admin"
    assert operations["forward"]["permission"] == "write"
    assert operations["forward"]["additional_permissions"] == ["read"]
    assert "private-group" not in str(result)
    assert "private-direct" not in str(result)
    assert "Synthetic private group" not in str(result)
    assert all(item["live_status"] == "blocked_compliance" for item in result["capabilities"])

    no_accessible_write_chat, _, no_write_principal = setup(
        database,
        write=Scope(
            enabled=True,
            allow_types=[ChatKind.PUBLIC_CHANNEL],
            exclude_chats=["public-channel"],
        ),
    )
    no_write_result = await Gateway(database, SyntheticAdapter()).capabilities(
        no_write_principal, no_accessible_write_chat
    )
    assert not any(
        item["permission"] == "write" and item["chat_required"]
        for item in no_write_result["capabilities"]
    )

    write_only_account, _, write_only_principal = setup(
        database,
        write=Scope(enabled=True, allow_chats=["public-channel"]),
    )
    write_only_result = await Gateway(database, SyntheticAdapter()).capabilities(
        write_only_principal, write_only_account
    )
    write_only_operations = {item["operation"] for item in write_only_result["capabilities"]}
    assert "send" in write_only_operations
    assert "forward" not in write_only_operations


@pytest.mark.asyncio
async def test_capability_discovery_fails_closed_without_chat_metadata(database):
    account_id, _, principal = setup(
        database,
        read=Scope(enabled=True, allow_types=[ChatKind.PUBLIC_CHANNEL]),
        write=Scope(enabled=True, allow_chats=["public-channel"]),
        admin=Scope(enabled=True, allow_chats=["public-group"]),
    )

    class UnavailableChatMetadataAdapter(SyntheticAdapter):
        async def capability_chats(self, selected_account_id):
            raise AdapterError("disconnected")

        async def reconnect(self, selected_account_id):
            raise AssertionError("capability discovery must not reconnect")

    result = await Gateway(database, UnavailableChatMetadataAdapter()).capabilities(
        principal, account_id
    )
    assert not any(item["chat_required"] for item in result["capabilities"])
    assert "public-channel" not in str(result)
    assert "Synthetic channel" not in str(result)
    assert result["live_adapter_enabled"] is False

    class LegacyAdapterWithoutChatMetadata:
        async def capability_profile(self, selected_account_id):
            return {}

        async def reconnect(self, selected_account_id):
            raise AssertionError("capability discovery must not reconnect")

    missing_hook_result = await Gateway(database, LegacyAdapterWithoutChatMetadata()).capabilities(
        principal, account_id
    )
    assert not any(item["chat_required"] for item in missing_hook_result["capabilities"])
    assert "public-channel" not in str(missing_hook_result)


@pytest.mark.asyncio
async def test_capability_discovery_rechecks_chat_policy_after_metadata_lookup(database):
    account_id, client_id, principal = setup(
        database,
        read=Scope(enabled=True, allow_types=[ChatKind.PUBLIC_CHANNEL]),
    )
    metadata_started = asyncio.Event()
    metadata_continue = asyncio.Event()

    class PausingChatAdapter(SyntheticAdapter):
        async def capability_chats(self, selected_account_id):
            metadata_started.set()
            await metadata_continue.wait()
            return await super().capability_chats(selected_account_id)

    gateway = Gateway(database, PausingChatAdapter())
    pending = asyncio.create_task(gateway.capabilities(principal, account_id))
    await asyncio.wait_for(metadata_started.wait(), timeout=2)
    with database() as session:
        grant = session.get(Grant, (client_id, account_id))
        grant.read_scope = Scope(
            enabled=True, allow_chats=["not-in-adapter-catalog"]
        ).model_dump(mode="json")
        session.commit()
    metadata_continue.set()

    result = await pending
    assert not any(item["chat_required"] for item in result["capabilities"])
    assert "public-channel" not in str(result)


@pytest.mark.asyncio
async def test_capability_discovery_rechecks_client_revocation_after_profile_lookup(database):
    account_id, client_id, principal = setup(database, read=Scope(enabled=True))
    profile_started = asyncio.Event()
    profile_continue = asyncio.Event()

    class PausingAdapter(SyntheticAdapter):
        async def capability_profile(self, selected_account_id):
            profile_started.set()
            await profile_continue.wait()
            return await super().capability_profile(selected_account_id)

    gateway = Gateway(database, PausingAdapter())
    pending = asyncio.create_task(gateway.capabilities(principal, account_id))
    await asyncio.wait_for(profile_started.wait(), timeout=2)
    with database() as session:
        client = session.get(Client, client_id)
        client.enabled = False
        session.commit()
    profile_continue.set()

    with pytest.raises(BatchRejected) as revoked:
        await pending
    assert revoked.value.status_code == 401
    assert revoked.value.code == "credential_revoked"
