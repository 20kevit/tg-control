"""Read-only operational diagnostics.

Reports *state*, never payloads: counts, states, ages, revisions and normalized
error codes. No message text, token, credential, phone number, path or SQL text
is included. Every finding is derived from a bounded query or an in-memory
counter, and the collector never mutates durable state.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import func, select, text
from sqlalchemy.exc import SQLAlchemyError

from .adapters.base import AdapterError, SessionUnavailable
from .db import (
    EXPECTED_SCHEMA_REVISION,
    Account,
    AuditEvent,
    EventDeadLetter,
    EventSubscription,
    Job,
    WorkflowRun,
)
from .observability import METRICS, SPANS

STUCK_JOB_SECONDS = 900
STUCK_WORKFLOW_SECONDS = 900
FAILED_JOB_LOOKBACK = timedelta(hours=24)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if isinstance(value, datetime) else None


def _age_seconds(value: datetime | None, now: datetime) -> int | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((now - value).total_seconds()))


class Diagnostics:
    def __init__(
        self,
        *,
        sessions,
        engine,
        gateway,
        instance_guard,
        plugins=None,
        monitor_progress=None,
        scheduler_task: Callable[[], Any] | None = None,
    ):
        self.sessions = sessions
        self.engine = engine
        self.gateway = gateway
        self.instance_guard = instance_guard
        self.plugins = plugins
        self.monitor_progress = monitor_progress
        self.scheduler_task = scheduler_task

    async def collect(self, *, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(timezone.utc)
        findings: list[dict[str, str]] = []
        report: dict[str, Any] = {
            "generated_at": now.isoformat(),
            "read_only": True,
            "database": self._database(findings),
            "jobs": self._jobs(now, findings),
            "workflows": self._workflows(now, findings),
            "events": self._events(findings),
            "plugins": self._plugins(findings),
            "accounts": await self._accounts(findings),
            "retention": self._retention(now),
            "runtime": self._runtime(findings),
            "findings": findings,
        }
        return report

    # -- sections ----------------------------------------------------------

    def _database(self, findings: list[dict[str, str]]) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            with self.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one_or_none()
        except SQLAlchemyError:
            findings.append(
                {"severity": "critical", "code": "database_unavailable", "detail": "db"}
            )
            return {"reachable": False, "migration_state": "unknown", "latency_ms": None}
        latency = round((time.perf_counter() - started) * 1000, 3)
        matches = revision == EXPECTED_SCHEMA_REVISION
        if not matches:
            findings.append(
                {
                    "severity": "critical",
                    "code": "migration_mismatch",
                    "detail": f"expected {EXPECTED_SCHEMA_REVISION}",
                }
            )
        return {
            "reachable": True,
            "migration_revision": revision,
            "expected_revision": EXPECTED_SCHEMA_REVISION,
            "migration_state": "current" if matches else "mismatch",
            "latency_ms": latency,
        }

    def _jobs(self, now: datetime, findings: list[dict[str, str]]) -> dict[str, Any]:
        section: dict[str, Any] = {
            "by_state": {},
            "overdue_leases": 0,
            "failed_last_24h": 0,
            "outcome_unknown": 0,
            "oldest_running_seconds": None,
        }
        try:
            with self.sessions() as session:
                rows = session.execute(
                    select(Job.state, func.count()).group_by(Job.state)
                ).all()
                section["by_state"] = {state: int(count) for state, count in rows}
                section["failed_last_24h"] = int(
                    session.scalar(
                        select(func.count()).where(
                            Job.state == "failed", Job.updated_at >= now - FAILED_JOB_LOOKBACK
                        )
                    )
                    or 0
                )
                section["outcome_unknown"] = int(
                    session.scalar(
                        select(func.count()).where(Job.state == "outcome_unknown")
                    )
                    or 0
                )
                expired = session.scalars(
                    select(Job).where(
                        Job.state == "running", Job.lease_expires_at < now
                    )
                ).all()
                section["overdue_leases"] = len(expired)
                running = session.scalars(
                    select(Job).where(Job.state == "running").order_by(Job.updated_at)
                ).first()
                if running is not None:
                    age = _age_seconds(running.updated_at, now)
                    section["oldest_running_seconds"] = age
                    if age is not None and age > STUCK_JOB_SECONDS:
                        findings.append(
                            {
                                "severity": "warning",
                                "code": "stuck_job",
                                "detail": f"running for {age}s",
                            }
                        )
        except SQLAlchemyError:
            findings.append(
                {"severity": "warning", "code": "job_diagnostics_unavailable", "detail": "db"}
            )
        if section["overdue_leases"]:
            findings.append(
                {
                    "severity": "warning",
                    "code": "overdue_job_leases",
                    "detail": str(section["overdue_leases"]),
                }
            )
        if section["outcome_unknown"]:
            findings.append(
                {
                    "severity": "warning",
                    "code": "unknown_job_outcomes",
                    "detail": str(section["outcome_unknown"]),
                }
            )
        if section["failed_last_24h"]:
            findings.append(
                {
                    "severity": "info",
                    "code": "recent_job_failures",
                    "detail": str(section["failed_last_24h"]),
                }
            )
        return section

    def _workflows(self, now: datetime, findings: list[dict[str, str]]) -> dict[str, Any]:
        section: dict[str, Any] = {"by_state": {}, "stuck_running": 0}
        try:
            with self.sessions() as session:
                rows = session.execute(
                    select(WorkflowRun.state, func.count()).group_by(WorkflowRun.state)
                ).all()
                section["by_state"] = {state: int(count) for state, count in rows}
                stuck = session.scalars(
                    select(WorkflowRun).where(
                        WorkflowRun.state == "running",
                        WorkflowRun.started_at < now - timedelta(seconds=STUCK_WORKFLOW_SECONDS),
                    )
                ).all()
                section["stuck_running"] = len(stuck)
        except SQLAlchemyError:
            findings.append(
                {
                    "severity": "warning",
                    "code": "workflow_diagnostics_unavailable",
                    "detail": "db",
                }
            )
        if section["stuck_running"]:
            findings.append(
                {
                    "severity": "warning",
                    "code": "stuck_workflow_runs",
                    "detail": str(section["stuck_running"]),
                }
            )
        unknown = section["by_state"].get("outcome_unknown", 0)
        if unknown:
            findings.append(
                {
                    "severity": "info",
                    "code": "interrupted_workflow_runs",
                    "detail": str(unknown),
                }
            )
        return section

    def _events(self, findings: list[dict[str, str]]) -> dict[str, Any]:
        section: dict[str, Any] = {
            "subscriptions": 0,
            "paused": 0,
            "dead_letters": 0,
        }
        try:
            with self.sessions() as session:
                section["subscriptions"] = int(
                    session.scalar(select(func.count()).select_from(EventSubscription)) or 0
                )
                section["paused"] = int(
                    session.scalar(
                        select(func.count()).where(EventSubscription.paused.is_(True))
                    )
                    or 0
                )
                section["dead_letters"] = int(
                    session.scalar(select(func.count()).select_from(EventDeadLetter)) or 0
                )
        except SQLAlchemyError:
            findings.append(
                {"severity": "warning", "code": "event_diagnostics_unavailable", "detail": "db"}
            )
        if section["dead_letters"]:
            findings.append(
                {
                    "severity": "info",
                    "code": "event_dead_letters",
                    "detail": str(section["dead_letters"]),
                }
            )
        return section

    def _plugins(self, findings: list[dict[str, str]]) -> dict[str, Any]:
        section: dict[str, Any] = {"installed": 0, "enabled": 0, "quarantined": 0}
        if self.plugins is None:
            return section
        try:
            entries = self.plugins.list()
        except Exception:
            findings.append(
                {
                    "severity": "warning",
                    "code": "plugin_diagnostics_unavailable",
                    "detail": "registry",
                }
            )
            return section
        section["installed"] = len(entries)
        section["enabled"] = sum(1 for entry in entries if entry["state"] == "enabled")
        section["quarantined"] = sum(1 for entry in entries if entry["state"] == "quarantined")
        if section["quarantined"]:
            findings.append(
                {
                    "severity": "warning",
                    "code": "plugin_quarantined",
                    "detail": str(section["quarantined"]),
                }
            )
        return section

    async def _accounts(self, findings: list[dict[str, str]]) -> dict[str, Any]:
        section: dict[str, Any] = {"enabled": 0, "adapter_states": {}, "unavailable": 0}
        try:
            with self.sessions() as session:
                accounts = session.scalars(
                    select(Account).where(Account.enabled.is_(True))
                ).all()
                section["enabled"] = len(accounts)
                for account in accounts[:50]:
                    try:
                        status = await asyncio.wait_for(
                            self.gateway.adapter.status(account.id), timeout=2
                        )
                    except (AdapterError, SessionUnavailable, TimeoutError):
                        section["unavailable"] += 1
                        continue
                    state = getattr(status, "state", "unknown")
                    section["adapter_states"][state] = (
                        section["adapter_states"].get(state, 0) + 1
                    )
        except SQLAlchemyError:
            findings.append(
                {"severity": "warning", "code": "account_diagnostics_unavailable", "detail": "db"}
            )
        needs_attention = sum(
            section["adapter_states"].get(state, 0)
            for state in ("reauth_required", "disconnected")
        )
        if needs_attention:
            findings.append(
                {
                    "severity": "warning",
                    "code": "account_sessions_need_attention",
                    "detail": str(needs_attention),
                }
            )
        if section["unavailable"]:
            findings.append(
                {
                    "severity": "warning",
                    "code": "adapter_unavailable",
                    "detail": str(section["unavailable"]),
                }
            )
        return section

    def _retention(self, now: datetime) -> dict[str, Any]:
        section: dict[str, Any] = {"oldest_audit_id": None, "audit_events": 0}
        try:
            with self.sessions() as session:
                section["audit_events"] = int(
                    session.scalar(select(func.count()).select_from(AuditEvent)) or 0
                )
                oldest = session.execute(
                    select(AuditEvent.id, AuditEvent.timestamp)
                    .order_by(AuditEvent.id)
                    .limit(1)
                ).first()
                if oldest is not None:
                    section["oldest_audit_id"] = int(oldest[0])
                    age = _age_seconds(oldest[1], now)
                    section["oldest_audit_age_seconds"] = age
        except SQLAlchemyError:
            section["audit_events"] = None
        try:
            section["storage"] = self.gateway.storage_status()
        except Exception:
            section["storage"] = {"available": False}
        return section

    def _runtime(self, findings: list[dict[str, str]]) -> dict[str, Any]:
        section: dict[str, Any] = {
            "instance_guard": "unknown",
            "session_monitor": None,
            "spans_recorded": len(SPANS.snapshot(limit=512)),
            "metrics_dropped_series": METRICS.snapshot()["dropped_series"],
        }
        try:
            section["instance_guard"] = "held" if self.instance_guard.check() else "lost"
        except Exception:
            section["instance_guard"] = "unknown"
        if section["instance_guard"] == "lost":
            findings.append(
                {"severity": "critical", "code": "instance_guard_lost", "detail": "guard"}
            )
        if self.monitor_progress is not None:
            try:
                section["session_monitor"] = self.monitor_progress.snapshot(task_running=True)
            except Exception:
                section["session_monitor"] = None
        return section
