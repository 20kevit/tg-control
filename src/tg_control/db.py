"""Database models and sessions. Gateway metadata does not contain message bodies."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.pool import StaticPool

# Bumped with every additive migration. Startup refuses anything else; it never
# auto-migrates. Downgrades require explicit approval and a verified backup.
EXPECTED_SCHEMA_REVISION = "0006"


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    alias: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    phone_encrypted: Mapped[str] = mapped_column(String(512), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )


class Client(Base):
    __tablename__ = "clients"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )


class Credential(Base):
    __tablename__ = "credentials"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(ForeignKey("clients.id", ondelete="CASCADE"), index=True)
    token_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Grant(Base):
    __tablename__ = "grants"
    client_id: Mapped[str] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), primary_key=True
    )
    read_scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    write_scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    admin_scope: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class Setting(Base):
    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    event: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_client_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    account_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    __table_args__ = (
        Index("ix_audit_timestamp_id", "timestamp", "id"),
        Index("ix_audit_event_id", "event", "id"),
    )


def make_engine(database_url: str) -> Engine:
    options: dict[str, Any] = {"pool_pre_ping": True, "hide_parameters": True}
    if database_url.startswith("sqlite"):
        options["connect_args"] = {"check_same_thread": False}
        if database_url in {"sqlite://", "sqlite:///:memory:"}:
            options["poolclass"] = StaticPool
    engine = create_engine(database_url, **options)
    if engine.dialect.name == "sqlite":

        @event.listens_for(engine, "connect")
        def _enable_foreign_keys(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def make_sessions(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)


class ExecutionKey(Base):
    """One-time mutation keys prevent accidental/replayed side effects per client."""

    __tablename__ = "execution_keys"
    client_id: Mapped[str] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(36), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )


class EventRecord(Base):
    """Normalized event inbox. Payloads are untrusted data, never instructions."""

    __tablename__ = "events"
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(36), unique=True, nullable=False, default=new_id)
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source: Mapped[str] = mapped_column(String(16), nullable=False)
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )
    aggregate_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    aggregate_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chat_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    causation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    __table_args__ = (
        Index("ix_events_account_seq", "account_id", "seq"),
        Index("ix_events_type_seq", "event_type", "seq"),
    )


class EventSubscription(Base):
    __tablename__ = "event_subscriptions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    client_id: Mapped[str] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    event_types: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    chat_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    max_inflight: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cursor_seq: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )
    __table_args__ = (
        Index("ix_event_subscriptions_client", "client_id", "account_id"),
        Index("ix_event_subscriptions_name", "client_id", "account_id", "name", unique=True),
    )


class EventAttempt(Base):
    __tablename__ = "event_attempts"
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("event_subscriptions.id", ondelete="CASCADE"), primary_key=True
    )
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EventDeadLetter(Base):
    __tablename__ = "event_dead_letters"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subscription_id: Mapped[str] = mapped_column(
        ForeignKey("event_subscriptions.id", ondelete="CASCADE"), index=True
    )
    event_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    event_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )
    __table_args__ = (
        Index("ix_event_dead_letters_event", "subscription_id", "event_id", unique=True),
    )


class Job(Base):
    """Durable job metadata. Never stores credentials, secrets or message bodies."""

    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(48), nullable=False)
    client_id: Mapped[str | None] = mapped_column(
        ForeignKey("clients.id", ondelete="CASCADE"), nullable=True, index=True
    )
    credential_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    account_id: Mapped[str | None] = mapped_column(
        ForeignKey("accounts.id", ondelete="CASCADE"), nullable=True
    )
    input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    schedule_kind: Mapped[str] = mapped_column(String(16), nullable=False, default="once")
    run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    local_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    misfire_policy: Mapped[str] = mapped_column(String(16), nullable=False, default="skip")
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paused: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_error: Mapped[str | None] = mapped_column(String(64), nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(36), unique=True, nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=now, onupdate=now
    )
    __table_args__ = (
        Index("ix_jobs_state_run_at", "state", "run_at"),
        Index("ix_jobs_client_state", "client_id", "state"),
    )


class JobRun(Base):
    __tablename__ = "job_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    job_id: Mapped[str] = mapped_column(
        ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    outcome_unknown: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    __table_args__ = (Index("ix_job_runs_job_attempt", "job_id", "attempt", unique=True),)
