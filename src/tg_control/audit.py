"""Audit metadata only. Never pass content, filenames, secrets or raw exceptions."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from .db import AuditEvent, now


def record(
    session: Session,
    *,
    request_id: str,
    actor_kind: str,
    actor_id: str | None,
    event: str,
    decision: str,
    account_id: str | None = None,
    subject_client_id: str | None = None,
    chat_id: str | None = None,
    error_code: str | None = None,
) -> None:
    session.add(
        AuditEvent(
            request_id=request_id,
            actor_kind=actor_kind,
            actor_id=actor_id,
            event=event,
            account_id=account_id,
            subject_client_id=subject_client_id,
            chat_id=chat_id,
            decision=decision,
            error_code=error_code,
        )
    )


def prune(session: Session, retention_days: int) -> int:
    if retention_days < 1:
        raise ValueError("audit retention must be at least one day")
    result = session.execute(
        delete(AuditEvent).where(AuditEvent.timestamp < now() - timedelta(days=retention_days))
    )
    return result.rowcount
