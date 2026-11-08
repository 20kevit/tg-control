"""Durable event delivery acceptance tests (synthetic fixtures only)."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, update

from tg_control.adapters.base import ChatKind, Update
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.auth import Principal, rotate_credential
from tg_control.db import Account, Client, Credential, EventRecord, EventSubscription, Grant
from tg_control.events import EventError, EventInput, EventService, SubscriptionInput
from tg_control.policy import Scope


def make_account(session, alias="event account"):
    account = Account(alias=alias, phone_encrypted="synthetic-encrypted-phone")
    session.add(account)
    session.commit()
    return account


def make_client(session, name="event client", read_chat_types=None, allow_chats=None):
    client = Client(name=name)
    session.add(client)
    session.commit()
    credential = rotate_credential(session, client.id)
    session.commit()
    principal = Principal(
        client_id=client.id,
        credential_id=session.scalar(
            select(Credential.id).where(
                Credential.client_id == client.id, Credential.revoked.is_(False)
            )
        ),
    )
    return client, principal, credential


def grant_read(session, client, account, *, types=None, chats=None, enabled=True):
    scope = Scope(
        enabled=enabled,
        allow_types=types if types is not None else [ChatKind.PUBLIC_CHANNEL],
        allow_chats=chats or [],
    )
    existing = session.get(Grant, (client.id, account.id))
    if existing is None:
        session.add(
            Grant(
                client_id=client.id,
                account_id=account.id,
                read_scope=scope.model_dump(mode="json"),
                write_scope=Scope().model_dump(mode="json"),
                admin_scope=Scope().model_dump(mode="json"),
            )
        )
    else:
        existing.read_scope = scope.model_dump(mode="json")
    session.commit()


def envelope(account, **overrides):
    fields = {
        "event_type": "message.new",
        "account_id": account.id,
        "source": "adapter",
        "chat_id": "public-channel",
        "payload": {"message_id": 3, "text": "synthetic body", "has_media": False},
        "upstream_id": "public-channel:3:new",
    }
    fields.update(overrides)
    return EventInput(**fields)


async def test_envelope_validation_rejects_malformed_and_oversized_input(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        disabled = make_account(session, alias="disabled account")
        disabled.enabled = False
        session.commit()
    with pytest.raises(EventError) as unknown_type:
        await service.ingest(envelope(account, event_type="telegram.raw"))
    assert unknown_type.value.code == "unknown_event_type"
    with pytest.raises(EventError) as version:
        await service.ingest(envelope(account, schema_version=2))
    assert version.value.code == "unsupported_schema_version"
    with pytest.raises(EventError) as smuggled:
        await service.ingest(
            envelope(account, payload={"message_id": 1, "raw_tdlib": {"@type": "updateNewMessage"}})
        )
    assert smuggled.value.code == "invalid_payload"
    with pytest.raises(EventError) as unknown_chat:
        await service.ingest(envelope(account, chat_id="not-a-chat"))
    assert unknown_chat.value.code == "unknown_chat"
    with pytest.raises(ValueError):
        envelope(account, payload={"message_id": 1, "text": "x" * 9000})
    with database() as session:
        assert session.scalar(select(func.count()).select_from(EventRecord)) == 0


async def test_account_story_event_is_allowlisted_and_requires_account_read_scope(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        client, principal, _ = make_client(session)
        grant_read(session, client, account, types=[ChatKind.PUBLIC_CHANNEL])

    story_event = EventInput(
        event_type="story.updated",
        account_id=account.id,
        source="adapter",
        payload={"story_id": "synthetic-story-1", "state": "created"},
        upstream_id="story:synthetic-story-1:created",
    )
    accepted = await service.ingest(story_event)
    assert accepted["status"] == "accepted"
    subscription = await service.create_subscription(
        principal,
        SubscriptionInput(
            account_id=account.id,
            name="account stories",
            event_types=["story.updated"],
        ),
    )
    pulled = await service.pull(principal, subscription["id"])
    assert len(pulled["events"]) == 1
    assert pulled["events"][0]["chat_id"] is None
    assert pulled["events"][0]["payload"]["story_id"] == "synthetic-story-1"

    with database() as session:
        _, no_read_principal, _ = make_client(session, name="no read event client")
    with pytest.raises(EventError) as denied:
        await service.create_subscription(
            no_read_principal,
            SubscriptionInput(
                account_id=account.id,
                name="no read scope",
                event_types=["story.updated"],
            ),
        )
    assert denied.value.code == "denied"

    with pytest.raises(EventError) as no_chat:
        await service.ingest(
            EventInput(
                event_type="reaction.updated",
                account_id=account.id,
                source="adapter",
                payload={"message_id": 1, "reaction": "👍", "count": 1},
            )
        )
    assert no_chat.value.code == "invalid_event"
    with pytest.raises(EventError) as extra_field:
        await service.ingest(
            story_event.model_copy(
                update={"payload": {"story_id": "x", "state": "created", "raw": "no"}}
            )
        )
    assert extra_field.value.code == "invalid_payload"


async def test_ingestion_is_durable_and_deduplicated(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
    first = await service.ingest(envelope(account))
    second = await service.ingest(envelope(account, payload={"message_id": 3, "text": "other"}))
    assert first["status"] == "accepted"
    assert second["status"] == "duplicate"
    assert second["event_id"] == first["event_id"]
    # A new service instance (process restart) still sees the persisted event.
    restarted = EventService(database, SyntheticAdapter())
    with database() as session:
        stored = session.scalar(select(EventRecord))
    assert stored is not None and stored.payload["message_id"] == 3
    with database() as session:
        account2 = make_account(session, alias="second account")
    isolated = await restarted.ingest(envelope(account2, chat_id="public-channel"))
    assert isolated["status"] == "accepted"
    assert isolated["event_id"] != first["event_id"]


async def test_subscription_requires_read_scope_and_resolvable_chats(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        other = make_account(session, alias="other account")
        client, principal, _ = make_client(session)
    payload = SubscriptionInput(account_id=account.id, name="inbox")
    for error_code in ("denied",):
        with pytest.raises(EventError) as denied:
            await service.create_subscription(principal, payload)
        assert denied.value.code == error_code
    with database() as session:
        grant_read(session, client, account, types=[ChatKind.PUBLIC_CHANNEL])
    with pytest.raises(EventError) as private_chat:
        await service.create_subscription(
            principal,
            SubscriptionInput(account_id=account.id, name="private", chat_ids=["private-direct"]),
        )
    assert private_chat.value.code == "unknown_chat"
    created = await service.create_subscription(
        principal,
        SubscriptionInput(
            account_id=account.id,
            name="channel only",
            event_types=["message.new"],
            chat_ids=["public-channel"],
            max_inflight=5,
        ),
    )
    assert created["cursor"] == 0 and created["event_types"] == ["message.new"]
    with pytest.raises(EventError) as duplicate:
        await service.create_subscription(
            principal,
            SubscriptionInput(account_id=account.id, name="channel only"),
        )
    assert duplicate.value.code == "duplicate_subscription"
    with pytest.raises(EventError) as wrong_account:
        await service.create_subscription(
            principal, SubscriptionInput(account_id=other.id, name="inbox")
        )
    assert wrong_account.value.code == "denied"
    with pytest.raises(ValueError):
        SubscriptionInput(account_id=account.id, name="bad", event_types=["unknown.type"])


async def test_pull_filters_and_rechecks_read_scope_before_delivery(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        client, principal, token = make_client(session)
        grant_read(
            session,
            client,
            account,
            types=[ChatKind.PUBLIC_CHANNEL],
            chats=["private-group"],
        )
    subscription = await service.create_subscription(
        principal,
        SubscriptionInput(
            account_id=account.id,
            name="inbox",
            event_types=["message.new", "message.edited"],
            chat_ids=["public-channel", "private-group"],
        ),
    )
    await service.ingest(envelope(account, chat_id="public-channel", upstream_id="a"))
    await service.ingest(
        envelope(
            account,
            chat_id="private-group",
            upstream_id="b",
            payload={"message_id": 4, "text": "private", "has_media": False},
        )
    )
    await service.ingest(
        envelope(account, event_type="message.deleted", upstream_id="c", payload={"message_id": 1})
    )
    page = await service.pull(principal, subscription["id"])
    assert sorted(event["payload"]["message_id"] for event in page["events"]) == [3, 4]
    # Losing the explicit private-chat grant removes that chat from delivery even
    # though the subscription filter still names it and the event row persists.
    with database() as session:
        grant_read(session, client, account, types=[ChatKind.PUBLIC_CHANNEL])
    blocked = await service.pull(principal, subscription["id"], limit=10)
    assert [event["payload"]["message_id"] for event in blocked["events"]] == [3]
    service.ack(principal, subscription["id"], page["events"][0]["sequence"])
    with database() as session:
        grant_read(session, client, account, types=[], enabled=False)
    with pytest.raises(EventError) as denied:
        await service.pull(principal, subscription["id"])
    assert denied.value.code == "denied"
    with database() as session:
        grant_read(session, client, account, types=[ChatKind.PUBLIC_CHANNEL])
        session.execute(
            update(Credential).where(Credential.id == principal.credential_id).values(revoked=True)
        )
        session.commit()
    with pytest.raises(EventError) as revoked:
        await service.pull(principal, subscription["id"])
    assert revoked.value.code == "credential_revoked"


async def test_cursor_persistence_ack_monotonicity_and_conflict(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        client, principal, _ = make_client(session)
        grant_read(session, client, account)
    subscription = await service.create_subscription(
        principal, SubscriptionInput(account_id=account.id, name="inbox")
    )
    await service.ingest(envelope(account, upstream_id="one"))
    await service.ingest(envelope(account, upstream_id="two", payload={"message_id": 9}))
    page = await service.pull(principal, subscription["id"])
    assert len(page["events"]) == 2
    first_seq = page["events"][0]["sequence"]
    acked = service.ack(principal, subscription["id"], first_seq)
    assert acked["cursor"] == first_seq
    # A restarted process resumes from the durable cursor, not from memory.
    restarted = EventService(database, SyntheticAdapter())
    resumed = await restarted.pull(principal, subscription["id"])
    assert [event["payload"]["message_id"] for event in resumed["events"]] == [9]
    with pytest.raises(EventError) as backwards:
        restarted.ack(principal, subscription["id"], 0)
    assert backwards.value.code == "invalid_cursor"
    with pytest.raises(EventError) as future:
        restarted.ack(principal, subscription["id"], first_seq + 1000)
    assert future.value.code == "invalid_cursor"
    final_seq = resumed["events"][0]["sequence"]
    restarted.ack(principal, subscription["id"], final_seq)
    empty = await restarted.pull(principal, subscription["id"])
    assert empty["events"] == [] and empty["has_more"] is False
    with pytest.raises(EventError) as not_owner:
        restarted.ack(
            Principal(client_id="00000000-0000-0000-0000-000000000000", credential_id="x"),
            subscription["id"],
            final_seq,
        )
    assert not_owner.value.code in {"credential_revoked", "subscription_not_found"}


async def test_backpressure_isolates_a_slow_consumer(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        client, principal, _ = make_client(session)
        grant_read(session, client, account)
    slow = await service.create_subscription(
        principal, SubscriptionInput(account_id=account.id, name="slow", max_inflight=2)
    )
    fast = await service.create_subscription(
        principal, SubscriptionInput(account_id=account.id, name="fast", max_inflight=100)
    )
    for index in range(6):
        await service.ingest(
            envelope(account, upstream_id=f"event-{index}", payload={"message_id": index + 1})
        )
    with pytest.raises(EventError) as pressure:
        await service.pull(principal, slow["id"])
    assert pressure.value.code == "backpressure"
    assert pressure.value.retry_after == 1
    delivered = await service.pull(principal, fast["id"])
    assert len(delivered["events"]) == 6


async def test_nack_retries_then_dead_letters_with_controlled_replay(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter, max_attempts=2)
    with database() as session:
        account = make_account(session)
        client, principal, _ = make_client(session)
        grant_read(session, client, account)
    subscription = await service.create_subscription(
        principal, SubscriptionInput(account_id=account.id, name="inbox")
    )
    await service.ingest(envelope(account, upstream_id="poison"))
    await service.ingest(envelope(account, upstream_id="healthy", payload={"message_id": 77}))
    page = await service.pull(principal, subscription["id"])
    poison = page["events"][0]
    first = service.nack(principal, subscription["id"], poison["event_id"], reason="consumer_error")
    assert first["status"] == "retry" and first["attempts"] == 1
    second = service.nack(principal, subscription["id"], poison["event_id"], reason="bogus reason")
    assert second["status"] == "dead_letter" and second["attempts"] == 2
    assert second["cursor"] == poison["sequence"]
    letters = service.dead_letters(subscription["id"])
    assert len(letters) == 1 and letters[0]["reason"] == "unspecified"
    # The poison event no longer blocks the stream.
    remaining = await service.pull(principal, subscription["id"])
    assert [event["payload"]["message_id"] for event in remaining["events"]] == [77]
    replay = service.replay_dead_letter(letters[0]["id"])
    assert replay["cursor"] == poison["sequence"] - 1
    assert service.dead_letters(subscription["id"]) == []
    with pytest.raises(EventError) as missing:
        service.replay_dead_letter("00000000-0000-0000-0000-000000000000")
    assert missing.value.code == "dead_letter_not_found"


async def test_adapter_update_drain_ingests_once_and_keeps_accounts_isolated(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        other = make_account(session, alias="other")
        client, principal, _ = make_client(session)
        grant_read(session, client, account)
        grant_read(session, client, other)
    await adapter.execute(account.id, "send", "public-channel", {"text": "synthetic send"})
    adapter.inject_update(
        Update(
            event_type="message.new",
            account_id=other.id,
            chat_id="public-channel",
            payload={"message_id": 42, "text": "other account", "has_media": False},
            upstream_id="other:42",
        )
    )
    first = await service.ingest_updates(account.id)
    assert first == {"accepted": 1, "duplicate": 0, "rejected": 0}
    second = await service.ingest_updates(account.id)
    assert second == {"accepted": 0, "duplicate": 0, "rejected": 0}
    other_result = await service.ingest_updates(other.id)
    assert other_result["accepted"] == 1
    subscription = await service.create_subscription(
        principal, SubscriptionInput(account_id=account.id, name="inbox")
    )
    page = await service.pull(principal, subscription["id"])
    assert len(page["events"]) == 1
    assert page["events"][0]["account_id"] == account.id
    assert page["events"][0]["payload"]["text"] == "synthetic send"
    # Buffers are per account: another account's pending update is never drained
    # or persisted under this account.
    adapter.inject_update(
        Update(
            event_type="message.new",
            account_id=other.id,
            chat_id="public-channel",
            payload={"message_id": 43, "has_media": False},
            upstream_id="injected",
        )
    )
    assert await service.ingest_updates(account.id) == {
        "accepted": 0,
        "duplicate": 0,
        "rejected": 0,
    }
    with database() as session:
        assert (
            session.scalar(select(EventRecord).where(EventRecord.account_id == account.id)).payload[
                "message_id"
            ]
            != 43
        )


async def test_operational_events_are_never_delivered_to_clients(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        client, principal, _ = make_client(session)
        grant_read(session, client, account)
    subscription = await service.create_subscription(
        principal, SubscriptionInput(account_id=account.id, name="inbox")
    )
    await service.ingest(
        EventInput(
            event_type="job.completed",
            account_id=account.id,
            source="scheduler",
            payload={"job_id": "job-1", "state": "succeeded"},
        )
    )
    page = await service.pull(principal, subscription["id"])
    assert page["events"] == []


async def test_paused_subscription_and_pruned_cursor(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        client, principal, _ = make_client(session)
        grant_read(session, client, account)
    subscription = await service.create_subscription(
        principal, SubscriptionInput(account_id=account.id, name="inbox")
    )
    service.set_paused(principal, subscription["id"], True)
    with pytest.raises(EventError) as paused:
        await service.pull(principal, subscription["id"])
    assert paused.value.code == "subscription_paused"
    resumed = service.set_paused(principal, subscription["id"], False)
    assert resumed["paused"] is False
    old = await service.ingest(envelope(account, upstream_id="pruned"))
    await service.ingest(envelope(account, upstream_id="fresh", payload={"message_id": 88}))
    with database() as session:
        session.execute(
            update(EventRecord)
            .where(EventRecord.id == old["event_id"])
            .values(observed_at=datetime.now(timezone.utc) - timedelta(days=10))
        )
        session.commit()
    assert service.prune(1) == 1
    with pytest.raises(EventError) as expired:
        await service.pull(principal, subscription["id"])
    assert expired.value.code == "cursor_expired"
    assert expired.value.cursor is not None and expired.value.cursor >= 1
    service.ack(principal, subscription["id"], expired.value.cursor)
    page = await service.pull(principal, subscription["id"])
    assert [event["payload"]["message_id"] for event in page["events"]] == [88]


async def test_subscription_deletion_removes_cursor_state(database):
    adapter = SyntheticAdapter()
    service = EventService(database, adapter)
    with database() as session:
        account = make_account(session)
        client, principal, _ = make_client(session)
        grant_read(session, client, account)
    subscription = await service.create_subscription(
        principal, SubscriptionInput(account_id=account.id, name="inbox")
    )
    await service.ingest(envelope(account, upstream_id="deleted"))
    page = await service.pull(principal, subscription["id"])
    service.nack(principal, subscription["id"], page["events"][0]["event_id"])
    service.delete_subscription(principal, subscription["id"])
    with database() as session:
        assert session.scalar(select(func.count()).select_from(EventSubscription)) == 0
    with pytest.raises(EventError) as missing:
        await service.pull(principal, subscription["id"])
    assert missing.value.code == "subscription_not_found"


def test_event_error_body_contains_no_upstream_detail():
    error = EventError("cursor_expired", 410, cursor=4)
    assert error.as_body() == {"error": "cursor_expired", "earliest_cursor": 4}
    assert str(uuid.uuid4())
