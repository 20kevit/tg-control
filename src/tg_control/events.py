"""Durable, normalized event ingestion and pull delivery (synthetic-only).

Design notes (see ADR-006):
- Events are persisted before acknowledgement. Deduplication uses a stable
  upstream identity when the adapter supplies one, otherwise a documented
  deterministic fingerprint of the normalized envelope.
- Delivery is pull-based: each subscription owns a durable monotonic cursor over
  the gateway event sequence. Delivery is at-least-once; consumers deduplicate by
  ``event_id``. Cursors only move forward and only through an explicit ack.
- Every delivery rechecks the credential and the current Read scope. Message
  content is never returned for a chat the client cannot currently read, and
  operational (non-adapter) events are never exposed to clients.
- Ordering is guaranteed only within one account's persisted sequence. No global
  Telegram ordering is implied.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import audit
from .adapters.base import (
    AdapterError,
    Chat,
    TelegramAdapter,
    Update,
)
from .auth import Principal, is_active
from .db import (
    Account,
    EventAttempt,
    EventDeadLetter,
    EventRecord,
    EventSubscription,
    new_id,
    now,
)
from .policy import ChatId, Permission, Scope, get_scope

SCHEMA_VERSION = 1
SUPPORTED_SCHEMA_VERSIONS = frozenset({1})
EVENT_SOURCES = frozenset({"adapter", "core", "command", "scheduler", "plugin"})

# Only schema-defined fields are accepted; unknown keys are rejected rather than
# stored, so an adapter cannot smuggle raw Telegram data into a public contract.
EVENT_PAYLOAD_KEYS: dict[str, frozenset[str]] = {
    "message.new": frozenset(
        {"message_id", "text", "text_truncated", "has_media", "media_kind", "sender_ref"}
    ),
    "message.edited": frozenset({"message_id", "text", "text_truncated"}),
    "message.deleted": frozenset({"message_id"}),
    "message.read": frozenset({"message_id", "up_to_id"}),
    "chat.discovered": frozenset({"title"}),
    "chat.updated": frozenset({"title"}),
    "chat.state_changed": frozenset({"state", "value"}),
    "reaction.updated": frozenset({"message_id", "reaction", "count"}),
    "poll.updated": frozenset({"message_id", "options_count", "closed"}),
    "member.updated": frozenset({"user_id", "role", "status"}),
    "topic.updated": frozenset({"topic_id", "title", "state"}),
    "story.updated": frozenset({"story_id", "state"}),
    "session.state": frozenset({"state", "intervention_required"}),
    "command.saved": frozenset(
        {
            "source_chat_id",
            "source_message_id",
            "destination_chat_id",
            "message_id",
            "has_media",
        }
    ),
    "job.completed": frozenset({"job_id", "state"}),
    "job.failed": frozenset({"job_id", "error"}),
    "workflow.completed": frozenset({"workflow_id", "run_id", "state"}),
    "plugin.health": frozenset({"plugin_id", "state"}),
}
EVENT_TYPES = frozenset(EVENT_PAYLOAD_KEYS)
# These adapter events contain only story identifiers/state and have no chat
# target. Delivery requires the account's current Read assignment; message and
# member content never inherit a chat scope.
ACCOUNT_EVENT_TYPES = frozenset({"story.updated"})
MAX_PAYLOAD_BYTES = 8192
MAX_EVENT_TYPES = 16
MAX_INFLIGHT_LIMIT = 1000
MAX_ATTEMPTS_LIMIT = 20
DEFAULT_MAX_ATTEMPTS = 5
PULL_SCAN_FACTOR = 8
PULL_SCAN_MAX = 800
DEAD_LETTER_REASONS = frozenset(
    {"consumer_error", "malformed_payload", "timeout", "unsupported", "unspecified"}
)

EventType = Annotated[
    str,
    Field(
        min_length=3,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){1,2}$",
    ),
]
Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


class EventInput(BaseModel):
    """Validated normalized event envelope supplied by an internal producer."""

    model_config = ConfigDict(extra="forbid")
    event_type: EventType
    schema_version: Annotated[int, Field(strict=True)] = SCHEMA_VERSION
    account_id: UUID
    source: Literal["adapter", "core", "command", "scheduler", "plugin"]
    occurred_at: Annotated[float, Field(ge=0)] | None = None
    aggregate_type: Annotated[str, Field(max_length=32, pattern=r"^[a-z_]+$")] | None = None
    aggregate_id: Identifier | None = None
    chat_id: ChatId | None = None
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    upstream_id: Identifier | None = None
    payload: dict[str, Any] = Field(default_factory=dict)

    @field_validator("payload")
    @classmethod
    def bounded_payload(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("payload must be an object")
        if len(value) > 16:
            raise ValueError("payload has too many fields")
        try:
            encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
        except (TypeError, ValueError) as exc:
            raise ValueError("payload must be JSON") from exc
        if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            raise ValueError("payload exceeds the event size limit")
        return value

    @property
    def fingerprint(self) -> str:
        if self.upstream_id is not None:
            material = f"{self.account_id}|{self.event_type}|{self.upstream_id}"
        else:
            # Documented deterministic fingerprint for sources without stable IDs:
            # two envelopes with identical normalized fields are the same event.
            canonical = {
                "account_id": str(self.account_id),
                "event_type": self.event_type,
                "aggregate_type": self.aggregate_type,
                "aggregate_id": self.aggregate_id,
                "chat_id": self.chat_id,
                "occurred_at": self.occurred_at,
                "payload": self.payload,
            }
            material = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(material.encode("utf-8")).hexdigest()


class SubscriptionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: UUID
    name: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _-]+$")]
    event_types: Annotated[list[EventType], Field(max_length=MAX_EVENT_TYPES)] = Field(
        default_factory=list
    )
    chat_ids: Annotated[list[ChatId], Field(max_length=128)] = Field(default_factory=list)
    max_inflight: Annotated[int, Field(strict=True, ge=1, le=MAX_INFLIGHT_LIMIT)] = 100

    @field_validator("event_types")
    @classmethod
    def known_types(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate event type")
        unknown = set(value) - EVENT_TYPES
        if unknown:
            raise ValueError("unknown event type")
        return value

    @field_validator("chat_ids")
    @classmethod
    def unique_chats(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate chat id")
        return value


class EventError(Exception):
    """Normalized event-domain failure; client-visible messages are codes only."""

    def __init__(
        self,
        code: str,
        status_code: int,
        *,
        cursor: int | None = None,
        retry_after: float | None = None,
    ):
        self.code = code
        self.status_code = status_code
        self.cursor = cursor
        self.retry_after = retry_after
        super().__init__(code)

    def as_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code}
        if self.cursor is not None:
            body["earliest_cursor"] = self.cursor
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body


def _unix_to_datetime(value: float | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise EventError("invalid_event", 422) from None


def _sanitized_reason(reason: str | None) -> str:
    return reason if reason in DEAD_LETTER_REASONS else "unspecified"


def serialize_event(event: EventRecord) -> dict[str, Any]:
    return {
        "event_id": event.id,
        "sequence": event.seq,
        "event_type": event.event_type,
        "schema_version": event.schema_version,
        "source": event.source,
        "account_id": event.account_id,
        "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
        "observed_at": event.observed_at.isoformat() if event.observed_at else None,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "chat_id": event.chat_id,
        "correlation_id": event.correlation_id,
        "causation_id": event.causation_id,
        "payload": event.payload,
    }


class EventService:
    def __init__(
        self,
        sessions: sessionmaker,
        adapter: TelegramAdapter,
        *,
        timeout_seconds: float = 30.0,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> None:
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise ValueError("invalid dead-letter attempt threshold")
        self.sessions = sessions
        self.adapter = adapter
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts

    # -- ingestion ---------------------------------------------------------

    def _validate_envelope(self, data: EventInput) -> None:
        if data.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise EventError("unsupported_schema_version", 422)
        if data.event_type not in EVENT_TYPES:
            raise EventError("unknown_event_type", 422)
        allowed = EVENT_PAYLOAD_KEYS[data.event_type]
        if not set(data.payload) <= allowed:
            # Unknown fields are dropped only by rejecting the event; never stored.
            raise EventError("invalid_payload", 422)
        if (
            data.source == "adapter"
            and data.chat_id is None
            and data.event_type not in ACCOUNT_EVENT_TYPES
        ):
            raise EventError("invalid_event", 422)

    async def ingest(self, data: EventInput, *, request_id: str | None = None) -> dict[str, Any]:
        self._validate_envelope(data)
        account_id = str(data.account_id)
        with self.sessions() as session:
            account = session.get(Account, account_id)
            if account is None or not account.enabled:
                raise EventError("unknown_account", 404)
        if data.chat_id is not None:
            try:
                chat = await asyncio.wait_for(
                    self.adapter.get_chat(account_id, data.chat_id), timeout=self.timeout_seconds
                )
            except (TimeoutError, AdapterError):
                raise EventError("unknown_chat", 422) from None
            except Exception:
                raise EventError("unknown_chat", 422) from None
            if not isinstance(chat, Chat) or chat.id != data.chat_id:
                raise EventError("unknown_chat", 422)
        record = EventRecord(
            id=str(uuid.uuid4()),
            account_id=account_id,
            event_type=data.event_type,
            schema_version=data.schema_version,
            source=data.source,
            occurred_at=_unix_to_datetime(data.occurred_at),
            observed_at=now(),
            aggregate_type=data.aggregate_type,
            aggregate_id=data.aggregate_id,
            chat_id=data.chat_id,
            correlation_id=str(data.correlation_id) if data.correlation_id else None,
            causation_id=str(data.causation_id) if data.causation_id else None,
            payload=data.payload,
            fingerprint=data.fingerprint,
        )
        with self.sessions() as session:
            session.add(record)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                existing = session.scalar(
                    select(EventRecord).where(EventRecord.fingerprint == data.fingerprint)
                )
                return {
                    "status": "duplicate",
                    "event_id": existing.id if existing else None,
                    "sequence": existing.seq if existing else None,
                }
            return {"status": "accepted", "event_id": record.id, "sequence": record.seq}

    async def ingest_updates(self, account_id: str, *, limit: int = 100) -> dict[str, int]:
        """Drain normalized adapter updates for one account into durable storage."""
        if not callable(getattr(self.adapter, "drain_updates", None)):
            # An adapter without an update stream is a valid, explicit capability gap.
            return {"accepted": 0, "duplicate": 0, "rejected": 0}
        try:
            updates: list[Update] = await asyncio.wait_for(
                self.adapter.drain_updates(account_id, limit), timeout=self.timeout_seconds
            )
        except (TimeoutError, AdapterError):
            return {"accepted": 0, "duplicate": 0, "rejected": 0}
        except Exception:
            return {"accepted": 0, "duplicate": 0, "rejected": 0}
        accepted = duplicate = rejected = 0
        for item in updates:
            try:
                envelope = EventInput(
                    event_type=item.event_type,
                    account_id=item.account_id,
                    source="adapter",
                    occurred_at=item.occurred_at,
                    aggregate_type=item.aggregate_type,
                    aggregate_id=item.aggregate_id,
                    chat_id=item.chat_id,
                    upstream_id=item.upstream_id,
                    payload=item.payload,
                )
                if str(envelope.account_id) != account_id:
                    rejected += 1
                    continue
                result = await self.ingest(envelope)
            except (EventError, ValueError):
                rejected += 1
                continue
            if result["status"] == "accepted":
                accepted += 1
            else:
                duplicate += 1
        return {"accepted": accepted, "duplicate": duplicate, "rejected": rejected}

    # -- subscriptions -----------------------------------------------------

    async def create_subscription(
        self, principal: Principal, data: SubscriptionInput, *, request_id: str | None = None
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        account_id = str(data.account_id)
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            account = session.get(Account, account_id)
            scope = get_scope(session, principal.client_id, account_id, Permission.READ)
            if account is None or not account.enabled or scope is None:
                raise EventError("denied", 403)
            scope_snapshot = Scope.model_validate(scope.model_dump())
        # Trusted chat metadata is resolved server-side; caller claims are never used.
        for chat_id in data.chat_ids:
            try:
                chat = await asyncio.wait_for(
                    self.adapter.get_chat(account_id, chat_id), timeout=self.timeout_seconds
                )
            except Exception:
                raise EventError("unknown_chat", 422) from None
            if not isinstance(chat, Chat) or chat.id != chat_id or not scope_snapshot.allows(chat):
                raise EventError("unknown_chat", 422)
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            current_scope = get_scope(session, principal.client_id, account_id, Permission.READ)
            if current_scope is None:
                raise EventError("denied", 403)
            subscription = EventSubscription(
                id=new_id(),
                client_id=principal.client_id,
                account_id=account_id,
                name=data.name,
                event_types=list(data.event_types),
                chat_ids=list(data.chat_ids),
                max_inflight=data.max_inflight,
            )
            session.add(subscription)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise EventError("duplicate_subscription", 409) from None
            audit.record(
                session,
                request_id=request_id,
                actor_kind="client",
                actor_id=principal.client_id,
                event="event_subscription_create",
                account_id=account_id,
                decision="success",
            )
            session.commit()
            return self._serialize_subscription(subscription)

    def list_subscriptions(self, principal: Principal) -> list[dict[str, Any]]:
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            rows = session.scalars(
                select(EventSubscription)
                .where(EventSubscription.client_id == principal.client_id)
                .order_by(EventSubscription.created_at, EventSubscription.id)
            ).all()
            return [self._serialize_subscription(row) for row in rows]

    @staticmethod
    def _serialize_subscription(row: EventSubscription) -> dict[str, Any]:
        return {
            "id": row.id,
            "account_id": row.account_id,
            "name": row.name,
            "event_types": list(row.event_types),
            "chat_ids": list(row.chat_ids),
            "max_inflight": row.max_inflight,
            "paused": row.paused,
            "cursor": row.cursor_seq,
        }

    def _owned(
        self, session: Session, principal: Principal, subscription_id: str
    ) -> EventSubscription:
        subscription = session.get(EventSubscription, subscription_id)
        if subscription is None or subscription.client_id != principal.client_id:
            raise EventError("subscription_not_found", 404)
        return subscription

    def delete_subscription(self, principal: Principal, subscription_id: str) -> None:
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            subscription = self._owned(session, principal, subscription_id)
            session.execute(
                delete(EventAttempt).where(EventAttempt.subscription_id == subscription.id)
            )
            session.execute(
                delete(EventDeadLetter).where(EventDeadLetter.subscription_id == subscription.id)
            )
            session.delete(subscription)
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="client",
                actor_id=principal.client_id,
                event="event_subscription_delete",
                account_id=subscription.account_id,
                decision="success",
            )
            session.commit()

    def set_paused(
        self, principal: Principal, subscription_id: str, paused: bool
    ) -> dict[str, Any]:
        if type(paused) is not bool:
            raise EventError("invalid_request", 422)
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            subscription = self._owned(session, principal, subscription_id)
            subscription.paused = paused
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="client",
                actor_id=principal.client_id,
                event="event_subscription_pause" if paused else "event_subscription_resume",
                account_id=subscription.account_id,
                decision="success",
            )
            session.commit()
            return self._serialize_subscription(subscription)

    # -- delivery ----------------------------------------------------------

    @staticmethod
    def _filtered(stmt, account_id: str, event_types: list[str], chat_ids: list[str]):
        stmt = stmt.where(EventRecord.account_id == account_id)
        if event_types:
            stmt = stmt.where(EventRecord.event_type.in_(list(event_types)))
        if chat_ids:
            stmt = stmt.where(EventRecord.chat_id.in_(list(chat_ids)))
        return stmt

    async def pull(
        self, principal: Principal, subscription_id: str, *, limit: int = 50
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise EventError("invalid_request", 422)
        rows: list[EventRecord] = []
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            subscription = self._owned(session, principal, subscription_id)
            if subscription.paused:
                raise EventError("subscription_paused", 409)
            account_id = subscription.account_id
            account = session.get(Account, account_id)
            scope = get_scope(session, principal.client_id, account_id, Permission.READ)
            if account is None or not account.enabled or scope is None:
                raise EventError("denied", 403)
            cursor = subscription.cursor_seq
            earliest = session.scalar(select(func.min(EventRecord.seq)))
            if earliest is None:
                return {"events": [], "cursor": cursor, "has_more": False, "earliest_cursor": 0}
            if cursor < earliest - 1:
                # Never silently skip pruned history; report the earliest resumable point.
                raise EventError("cursor_expired", 410, cursor=earliest - 1)
            backlog = session.scalar(
                self._filtered(
                    select(func.count()).select_from(EventRecord),
                    subscription.account_id,
                    list(subscription.event_types),
                    list(subscription.chat_ids),
                ).where(EventRecord.seq > cursor)
            )
            if backlog is not None and backlog > subscription.max_inflight:
                raise EventError("backpressure", 429, retry_after=1)
            statement = (
                self._filtered(
                    select(EventRecord),
                    subscription.account_id,
                    list(subscription.event_types),
                    list(subscription.chat_ids),
                )
                .where(EventRecord.seq > cursor)
                .order_by(EventRecord.seq)
                .limit(min(limit * PULL_SCAN_FACTOR, PULL_SCAN_MAX))
            )
            rows = list(session.scalars(statement).all())
            subscription_snapshot = self._serialize_subscription(subscription)
        delivered: list[dict[str, Any]] = []
        last_seq = rows[-1].seq if rows else cursor
        for row in rows:
            if len(delivered) >= limit:
                break
            if await self._deliverable(principal, subscription_snapshot, row):
                delivered.append(serialize_event(row))
                last_seq = row.seq
        # Final synchronous recheck after all adapter awaits, before returning content.
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            scope = get_scope(
                session, principal.client_id, subscription_snapshot["account_id"], Permission.READ
            )
            if scope is None:
                raise EventError("denied", 403)
            remaining = session.scalar(
                self._filtered(
                    select(func.count()).select_from(EventRecord),
                    subscription_snapshot["account_id"],
                    subscription_snapshot["event_types"],
                    subscription_snapshot["chat_ids"],
                ).where(EventRecord.seq > (last_seq if last_seq is not None else -1))
            )
        return {
            "events": delivered,
            "cursor": subscription_snapshot["cursor"],
            "has_more": bool(remaining),
            "earliest_cursor": 0,
        }

    async def _deliverable(
        self, principal: Principal, subscription: dict[str, Any], row: EventRecord
    ) -> bool:
        # Operational events are owner-only and never delivered to clients.
        if row.source != "adapter":
            return False
        if row.chat_id is None:
            if row.event_type not in ACCOUNT_EVENT_TYPES:
                return False
            with self.sessions() as session:
                if not is_active(session, principal):
                    return False
                account = session.get(Account, subscription["account_id"])
                scope = get_scope(
                    session, principal.client_id, subscription["account_id"], Permission.READ
                )
                return bool(account and account.enabled and scope)
        try:
            chat = await asyncio.wait_for(
                self.adapter.get_chat(row.account_id, row.chat_id), timeout=self.timeout_seconds
            )
        except Exception:
            return False
        if not isinstance(chat, Chat) or chat.id != row.chat_id:
            return False
        with self.sessions() as session:
            if not is_active(session, principal):
                return False
            scope = get_scope(
                session, principal.client_id, subscription["account_id"], Permission.READ
            )
            return bool(scope and scope.allows(chat))

    def ack(self, principal: Principal, subscription_id: str, sequence: int) -> dict[str, Any]:
        if type(sequence) is not int or sequence < 0:
            raise EventError("invalid_cursor", 422)
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            subscription = self._owned(session, principal, subscription_id)
            if sequence < subscription.cursor_seq:
                raise EventError("invalid_cursor", 422, cursor=subscription.cursor_seq)
            latest = session.scalar(select(func.max(EventRecord.seq)))
            if latest is not None and sequence > latest:
                raise EventError("invalid_cursor", 422, cursor=subscription.cursor_seq)
            previous = subscription.cursor_seq
            if sequence == previous:
                return {"cursor": previous}
            result = session.execute(
                update(EventSubscription)
                .where(
                    EventSubscription.id == subscription.id,
                    EventSubscription.cursor_seq == previous,
                )
                .values(cursor_seq=sequence)
            )
            if result.rowcount != 1:
                session.rollback()
                raise EventError("cursor_conflict", 409, cursor=previous)
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="client",
                actor_id=principal.client_id,
                event="event_ack",
                account_id=subscription.account_id,
                decision="success",
            )
            session.commit()
            return {"cursor": sequence}

    def nack(
        self,
        principal: Principal,
        subscription_id: str,
        event_id: str,
        *,
        reason: str | None = None,
    ) -> dict[str, Any]:
        if type(event_id) is not str or len(event_id) != 36:
            raise EventError("event_not_found", 404)
        with self.sessions() as session:
            if not is_active(session, principal):
                raise EventError("credential_revoked", 401)
            subscription = self._owned(session, principal, subscription_id)
            event = session.scalar(select(EventRecord).where(EventRecord.id == event_id))
            if event is None or event.account_id != subscription.account_id:
                raise EventError("event_not_found", 404)
            attempt = session.get(EventAttempt, (subscription.id, event.id))
            attempts = (attempt.attempts if attempt else 0) + 1
            clean_reason = _sanitized_reason(reason)
            if attempts >= self.max_attempts:
                existing = session.scalar(
                    select(EventDeadLetter).where(
                        EventDeadLetter.subscription_id == subscription.id,
                        EventDeadLetter.event_id == event.id,
                    )
                )
                if existing is None:
                    session.add(
                        EventDeadLetter(
                            id=new_id(),
                            subscription_id=subscription.id,
                            event_seq=event.seq,
                            event_id=event.id,
                            attempts=attempts,
                            reason=clean_reason,
                        )
                    )
                if attempt is not None:
                    session.delete(attempt)
                # Poison isolation: the consumer's cursor moves past the event so a
                # single bad event cannot block the rest of the stream.
                if event.seq > subscription.cursor_seq:
                    session.execute(
                        update(EventSubscription)
                        .where(EventSubscription.id == subscription.id)
                        .values(cursor_seq=event.seq)
                    )
                status = "dead_letter"
                event_name = "event_dead_letter"
            else:
                if attempt is None:
                    session.add(
                        EventAttempt(
                            subscription_id=subscription.id,
                            event_id=event.id,
                            attempts=attempts,
                            last_error=clean_reason,
                        )
                    )
                else:
                    attempt.attempts = attempts
                    attempt.last_error = clean_reason
                status = "retry"
                event_name = "event_retry"
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="client",
                actor_id=principal.client_id,
                event=event_name,
                account_id=subscription.account_id,
                chat_id=event.chat_id,
                decision=status,
                error_code=clean_reason if status == "dead_letter" else None,
            )
            session.commit()
            return {
                "status": status,
                "attempts": attempts,
                "max_attempts": self.max_attempts,
                "cursor": subscription.cursor_seq,
            }

    # -- owner operations --------------------------------------------------

    def dead_letters(self, subscription_id: str | None = None) -> list[dict[str, Any]]:
        with self.sessions() as session:
            statement = select(EventDeadLetter).order_by(
                EventDeadLetter.created_at, EventDeadLetter.id
            )
            if subscription_id is not None:
                statement = statement.where(EventDeadLetter.subscription_id == subscription_id)
            return [
                {
                    "id": row.id,
                    "subscription_id": row.subscription_id,
                    "event_id": row.event_id,
                    "sequence": row.event_seq,
                    "attempts": row.attempts,
                    "reason": row.reason,
                }
                for row in session.scalars(statement).all()
            ]

    def replay_dead_letter(self, dead_letter_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            dead_letter = session.get(EventDeadLetter, dead_letter_id)
            if dead_letter is None:
                raise EventError("dead_letter_not_found", 404)
            subscription = session.get(EventSubscription, dead_letter.subscription_id)
            if subscription is None:
                raise EventError("dead_letter_not_found", 404)
            session.delete(dead_letter)
            session.execute(
                delete(EventAttempt).where(
                    EventAttempt.subscription_id == subscription.id,
                    EventAttempt.event_id == dead_letter.event_id,
                )
            )
            previous = subscription.cursor_seq
            target = max(0, dead_letter.event_seq - 1)
            if previous >= dead_letter.event_seq:
                result = session.execute(
                    update(EventSubscription)
                    .where(
                        EventSubscription.id == subscription.id,
                        EventSubscription.cursor_seq == previous,
                    )
                    .values(cursor_seq=target)
                )
                if result.rowcount != 1:
                    session.rollback()
                    raise EventError("cursor_conflict", 409)
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="owner",
                actor_id=None,
                event="event_dead_letter_replay",
                account_id=subscription.account_id,
                subject_client_id=subscription.client_id,
                decision="success",
            )
            session.commit()
            return {"subscription_id": subscription.id, "cursor": target}

    def prune(self, retention_days: int) -> int:
        if type(retention_days) is not int or not 1 <= retention_days <= 36500:
            raise ValueError("invalid event retention")
        cutoff = now() - timedelta(days=retention_days)
        with self.sessions() as session:
            result = session.execute(delete(EventRecord).where(EventRecord.observed_at < cutoff))
            deleted = result.rowcount or 0
            if deleted:
                audit.record(
                    session,
                    request_id=str(uuid.uuid4()),
                    actor_kind="system",
                    actor_id=None,
                    event="event_retention",
                    decision="success",
                )
            session.commit()
            return deleted

    def earliest_cursor(self) -> int:
        with self.sessions() as session:
            earliest = session.scalar(select(func.min(EventRecord.seq)))
            return max(0, (earliest or 1) - 1)
