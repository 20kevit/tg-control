"""Durable jobs and a timezone-aware scheduler (synthetic-only).

Design notes (see ADR-007):
- Every state transition is validated and persisted transactionally.
- Work is claimed with a bounded lease using a conditional update, so a second
  worker cannot claim the same job. The repository remains single-instance; the
  lease protects against restart/hang, not multi-worker deployment.
- Only failures that prove no effect was applied are retried. Timeouts, lost
  leases and ambiguous adapter failures resolve to ``outcome_unknown``, which is
  terminal for automatic execution.
- Delayed operations revalidate the client credential and the current policy
  immediately before the effect (the registered handler performs the Core call,
  which rechecks authorization at execution time).
- Recurring local schedules use IANA zones. Nonexistent local times are skipped
  and repeated local times execute once (fold=0), unless a misfire policy says
  otherwise. Catch-up is bounded to one run.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from typing import Annotated, Any, Awaitable, Callable, Literal
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import audit
from .adapters.base import AdapterError, RateLimited, normalized_error_code
from .auth import Principal, is_active
from .db import Job, JobRun, now

SCHEMA_VERSION = 1
JOB_STATES = frozenset(
    {
        "queued",
        "scheduled",
        "running",
        "retry_wait",
        "succeeded",
        "failed",
        "outcome_unknown",
        "cancelled",
        "expired",
    }
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "outcome_unknown", "cancelled", "expired"})
CLAIMABLE_STATES = frozenset({"queued", "scheduled", "retry_wait"})
TRANSITIONS = {
    ("queued", "scheduled"),
    ("queued", "running"),
    ("queued", "cancelled"),
    ("queued", "expired"),
    ("scheduled", "queued"),
    ("scheduled", "running"),
    ("scheduled", "cancelled"),
    ("scheduled", "expired"),
    ("retry_wait", "running"),
    ("retry_wait", "scheduled"),
    ("retry_wait", "cancelled"),
    ("retry_wait", "expired"),
    ("retry_wait", "failed"),
    ("running", "succeeded"),
    ("running", "failed"),
    ("running", "outcome_unknown"),
    ("running", "retry_wait"),
    ("running", "cancelled"),
    ("running", "expired"),
    # A cancel request is recorded on the job, not as a separate terminal state.
    ("running", "scheduled"),
}
MISFIRE_POLICIES = frozenset({"skip", "run_once"})
SCHEDULE_KINDS = frozenset({"once", "daily_local"})
MAX_INPUT_BYTES = 4096
MAX_RESULT_BYTES = 4096
MAX_ATTEMPTS_LIMIT = 10
MAX_FIELD_KEYS = 16


class JobError(Exception):
    """Normalized job-domain failure; no upstream text is exposed."""

    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class CancelledNoEffect(Exception):
    """Raised by a handler that observed a cancellation before any effect."""


def validate_transition(current: str, target: str) -> None:
    if current not in JOB_STATES or target not in JOB_STATES:
        raise ValueError("unknown job state")
    if (current, target) not in TRANSITIONS:
        raise ValueError("invalid job state transition")


def transition(session: Session, job: Job, target: str) -> None:
    validate_transition(job.state, target)
    job.state = target


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timezone-aware datetimes are required")
    return value.astimezone(timezone.utc)


def _as_aware_utc(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; all stored instants are UTC by contract."""
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else _utc(value)


def _bounded_json(value: dict[str, Any], limit: int) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("expected an object")
    if len(value) > MAX_FIELD_KEYS:
        raise ValueError("too many fields")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError("not JSON serializable") from exc
    if len(encoded.encode("utf-8")) > limit:
        raise ValueError("payload exceeds the size limit")
    return value


def _timezone(name: str) -> ZoneInfo:
    if not isinstance(name, str) or not 1 <= len(name) <= 64:
        raise JobError("invalid_schedule", 422)
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise JobError("invalid_schedule", 422) from None


def parse_local_time(value: str) -> time:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        raise JobError("invalid_schedule", 422)
    try:
        hour, minute = int(value[:2]), int(value[3:])
    except ValueError:
        raise JobError("invalid_schedule", 422) from None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise JobError("invalid_schedule", 422)
    return time(hour, minute)


def next_local_occurrence(
    after: datetime, timezone_name: str, local_time: str, *, max_days: int = 8
) -> datetime:
    """Next valid occurrence strictly after ``after`` (UTC aware).

    Nonexistent local times (spring-forward gaps) are skipped. Repeated local
    times (autumn fall-back) use the first occurrence (fold=0), so a schedule
    executes once per wall-clock slot.
    """
    zone = _timezone(timezone_name)
    target = parse_local_time(local_time)
    reference = _utc(after).astimezone(zone)
    for offset in range(max_days + 1):
        day = reference.date() + timedelta(days=offset)
        naive = datetime.combine(day, target)
        candidate = naive.replace(tzinfo=zone, fold=0)
        # Round-trip detection: a nonexistent wall time normalizes to another time.
        if candidate.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
            continue
        # The offsets must be unambiguous enough to be a real local instant.
        if candidate.utcoffset() is None:
            continue
        if candidate > reference:
            return candidate.astimezone(timezone.utc)
    raise JobError("invalid_schedule", 422)


class Schedule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["once", "daily_local"] = "once"
    run_at: datetime | None = None
    timezone: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    local_time: Annotated[str, Field(min_length=5, max_length=5)] | None = None
    misfire_policy: Literal["skip", "run_once"] = "skip"
    misfire_grace_seconds: Annotated[int, Field(strict=True, ge=0, le=86400)] = 300

    @field_validator("run_at")
    @classmethod
    def aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("run_at must include an offset")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.kind == "once":
            if self.run_at is None:
                raise ValueError("once schedules require run_at")
            if self.timezone is not None or self.local_time is not None:
                raise ValueError("once schedules cannot carry a local time")
        else:
            if self.timezone is None or self.local_time is None:
                raise ValueError("daily_local schedules require timezone and local_time")
            if self.run_at is not None:
                raise ValueError("daily_local schedules derive run_at")
            _timezone(self.timezone)
            parse_local_time(self.local_time)
        return self

    def first_run(self, reference: datetime) -> datetime:
        if self.kind == "once":
            assert self.run_at is not None
            return _utc(self.run_at)
        assert self.timezone is not None and self.local_time is not None
        return next_local_occurrence(reference, self.timezone, self.local_time)


class JobOutcome(BaseModel):
    """Handler result. Unknown outcomes must be reported explicitly."""

    model_config = ConfigDict(extra="forbid")
    state: Literal[
        "succeeded", "failed", "outcome_unknown", "retry_wait", "cancelled", "expired"
    ]
    error: Annotated[str, Field(max_length=64, pattern=r"^[a-z_]+$")] | None = None
    retry_after: Annotated[float, Field(ge=0)] | None = None
    result: dict[str, Any] | None = None

    @field_validator("result")
    @classmethod
    def bounded(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return _bounded_json(value, MAX_RESULT_BYTES)


@dataclass
class JobContext:
    job_id: str
    kind: str
    attempt: int
    input: dict[str, Any]
    client_id: str | None
    credential_id: str | None
    account_id: str | None
    correlation_id: str | None
    worker_id: str
    cancel_requested: Callable[[], bool] = field(default=lambda: False)

    def check_cancelled(self) -> None:
        if self.cancel_requested():
            raise CancelledNoEffect()


@dataclass(frozen=True)
class JobKind:
    """A registered, explicitly typed job kind."""

    name: str
    input_model: type[BaseModel]
    handler: Callable[[JobContext], Awaitable[JobOutcome]]
    mutating: bool = True
    authorizer: Callable[[Principal, Any], Awaitable[None]] | None = None


def serialize_job(job: Job, runs: list[JobRun] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "job_id": job.id,
        "kind": job.kind,
        "state": job.state,
        "attempts": job.attempts,
        "max_attempts": job.max_attempts,
        "run_at": _as_aware_utc(job.run_at).isoformat() if job.run_at else None,
        "schedule_kind": job.schedule_kind,
        "timezone": job.timezone,
        "local_time": job.local_time,
        "misfire_policy": job.misfire_policy,
        "paused": job.paused,
        "cancel_requested": job.cancel_requested,
        "deadline": _as_aware_utc(job.deadline).isoformat() if job.deadline else None,
        "last_error": job.last_error,
        "result": job.result,
        "correlation_id": job.correlation_id,
        "account_id": job.account_id,
        "created_at": _as_aware_utc(job.created_at).isoformat() if job.created_at else None,
    }
    if runs is not None:
        payload["runs"] = [
            {
                "attempt": run.attempt,
                "state": run.state,
                "started_at": _as_aware_utc(run.started_at).isoformat(),
                "finished_at": (
                    _as_aware_utc(run.finished_at).isoformat() if run.finished_at else None
                ),
                "error": run.error_code,
                "outcome_unknown": run.outcome_unknown,
            }
            for run in runs
        ]
    return payload


class Jobs:
    def __init__(
        self,
        sessions: sessionmaker,
        *,
        handler_timeout_seconds: float = 30.0,
        lease_seconds: int = 60,
        clock: Callable[[], datetime] = now,
    ) -> None:
        if not 0 < handler_timeout_seconds <= 300:
            raise ValueError("invalid job handler timeout")
        if type(lease_seconds) is not int or not 5 <= lease_seconds <= 3600:
            raise ValueError("invalid lease duration")
        self.sessions = sessions
        self.handler_timeout_seconds = handler_timeout_seconds
        self.lease_seconds = lease_seconds
        self.clock = clock
        self._kinds: dict[str, JobKind] = {}
        self._closed = False

    # -- registry ----------------------------------------------------------

    def register(self, kind: JobKind) -> None:
        if not isinstance(kind, JobKind):
            raise TypeError("invalid job kind")
        simple = kind.name.replace(".", "").replace("_", "")
        if not (1 <= len(kind.name) <= 48) or not simple.isalnum():
            raise ValueError("invalid job kind name")
        if kind.name in self._kinds:
            raise ValueError("duplicate job kind")
        if not callable(getattr(kind.handler, "__call__", None)):
            raise ValueError("invalid job handler")
        self._kinds[kind.name] = kind

    def registered(self) -> list[str]:
        return sorted(self._kinds)

    def close(self) -> None:
        """Stop claiming new work. In-flight handlers finish within their timeout."""
        self._closed = True

    def _kind(self, name: str) -> JobKind:
        kind = self._kinds.get(name)
        if kind is None:
            raise JobError("unknown_job_kind", 422)
        return kind

    # -- creation ----------------------------------------------------------

    async def enqueue(
        self,
        principal: Principal,
        kind_name: str,
        payload: dict[str, Any],
        *,
        schedule: Schedule | None = None,
        idempotency_key: UUID | None = None,
        deadline_seconds: int = 3600,
        max_attempts: int = 3,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if self._closed:
            raise JobError("scheduler_unavailable", 503)
        if type(max_attempts) is not int or not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise JobError("invalid_request", 422)
        if type(deadline_seconds) is not int or not 30 <= deadline_seconds <= 86400:
            raise JobError("invalid_request", 422)
        request_id = request_id or str(uuid.uuid4())
        kind = self._kind(kind_name)
        try:
            parsed = kind.input_model.model_validate(payload)
        except ValueError:
            raise JobError("invalid_input", 422) from None
        schedule = schedule or Schedule(kind="once", run_at=self.clock() + timedelta(seconds=60))
        created = self.clock()
        run_at = schedule.first_run(created)
        with self.sessions() as session:
            if not is_active(session, principal):
                raise JobError("credential_revoked", 401)
        if kind.authorizer is not None:
            try:
                await kind.authorizer(principal, parsed)
            except JobError:
                raise
            except Exception:
                raise JobError("denied", 403) from None
        account_id = getattr(parsed, "account_id", None)
        job = Job(
            id=str(uuid.uuid4()),
            schema_version=SCHEMA_VERSION,
            kind=kind.name,
            client_id=principal.client_id,
            credential_id=principal.credential_id,
            account_id=str(account_id) if account_id is not None else None,
            input=parsed.model_dump(mode="json"),
            state="scheduled" if run_at > created else "queued",
            schedule_kind=schedule.kind,
            run_at=run_at,
            timezone=schedule.timezone,
            local_time=schedule.local_time,
            misfire_policy=schedule.misfire_policy,
            misfire_grace_seconds=schedule.misfire_grace_seconds,
            attempts=0,
            max_attempts=max_attempts,
            deadline=created + timedelta(seconds=deadline_seconds),
            idempotency_key=str(idempotency_key) if idempotency_key else None,
            correlation_id=request_id,
        )
        with self.sessions() as session:
            session.add(job)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise JobError("duplicate_request", 409) from None
            audit.record(
                session,
                request_id=request_id,
                actor_kind="client",
                actor_id=principal.client_id,
                event="job_enqueue",
                account_id=job.account_id,
                decision="success",
            )
            session.commit()
            return serialize_job(job)

    # -- inspection and lifecycle -----------------------------------------

    def _owned_job(
        self, session: Session, principal: Principal | None, job_id: str
    ) -> Job:
        if not isinstance(job_id, str) or len(job_id) != 36:
            raise JobError("job_not_found", 404)
        job = session.get(Job, job_id)
        if job is None:
            raise JobError("job_not_found", 404)
        if principal is not None and job.client_id != principal.client_id:
            # Never reveal the existence of another client's job.
            raise JobError("job_not_found", 404)
        return job

    def prune_terminal(
        self,
        *,
        older_than_days: int,
        dry_run: bool = False,
        reference: datetime | None = None,
    ) -> dict[str, Any]:
        """Delete terminal job records older than the retention window.

        Only rows in a terminal state are eligible: a queued, scheduled, running
        or retry-waiting job is operational state, not history. Non-idempotent
        side effects are never retried because a record was pruned.
        """
        if type(older_than_days) is not int or not 1 <= older_than_days <= 36500:
            raise JobError("invalid_request", 422)
        if type(dry_run) is not bool:
            raise JobError("invalid_request", 422)
        cutoff = (reference or self.clock()) - timedelta(days=older_than_days)
        with self.sessions() as session:
            predicate = and_(
                Job.state.in_(sorted(TERMINAL_STATES)),
                Job.updated_at < cutoff,
            )
            eligible = session.scalar(select(func.count()).select_from(Job).where(predicate)) or 0
            deleted = 0
            if not dry_run and eligible:
                deleted = session.execute(delete(Job).where(predicate)).rowcount or 0
                if deleted:
                    audit.record(
                        session,
                        request_id=str(uuid.uuid4()),
                        actor_kind="system",
                        actor_id=None,
                        event="jobs_pruned",
                        decision="allow",
                    )
                session.commit()
            return {
                "state": "pruned",
                "dry_run": dry_run,
                "eligible": int(eligible),
                "deleted": int(deleted),
                "retention_days": older_than_days,
                "cutoff": cutoff.isoformat(),
            }

    def inspect(
        self, principal: Principal | None, job_id: str
    ) -> dict[str, Any]:
        with self.sessions() as session:
            if principal is not None and not is_active(session, principal):
                raise JobError("credential_revoked", 401)
            job = self._owned_job(session, principal, job_id)
            runs = list(
                session.scalars(
                    select(JobRun).where(JobRun.job_id == job.id).order_by(JobRun.attempt)
                ).all()
            )
            return serialize_job(job, runs)

    def list_jobs(
        self,
        principal: Principal | None,
        *,
        limit: int = 50,
        before: tuple[datetime, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Stable keyset listing: newest first, (created_at, id) tiebreaker."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise JobError("invalid_request", 422)
        with self.sessions() as session:
            statement = (
                select(Job)
                .order_by(Job.created_at.desc(), Job.id.desc())
                .limit(limit)
            )
            if principal is not None:
                if not is_active(session, principal):
                    raise JobError("credential_revoked", 401)
                statement = statement.where(Job.client_id == principal.client_id)
            if before is not None:
                created_at, identifier = before
                statement = statement.where(
                    or_(
                        Job.created_at < created_at,
                        and_(Job.created_at == created_at, Job.id < identifier),
                    )
                )
            return [serialize_job(job) for job in session.scalars(statement).all()]

    def set_paused(
        self, principal: Principal | None, job_id: str, paused: bool
    ) -> dict[str, Any]:
        if type(paused) is not bool:
            raise JobError("invalid_request", 422)
        with self.sessions() as session:
            if principal is not None and not is_active(session, principal):
                raise JobError("credential_revoked", 401)
            job = self._owned_job(session, principal, job_id)
            if job.state in TERMINAL_STATES:
                raise JobError("job_terminal", 409)
            job.paused = paused
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="client" if principal is not None else "owner",
                actor_id=principal.client_id if principal is not None else None,
                event="job_pause" if paused else "job_resume",
                account_id=job.account_id,
                decision="success",
            )
            session.commit()
            return serialize_job(job)

    def cancel(self, principal: Principal | None, job_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            if principal is not None and not is_active(session, principal):
                raise JobError("credential_revoked", 401)
            job = self._owned_job(session, principal, job_id)
            if job.state in TERMINAL_STATES:
                raise JobError("job_terminal", 409)
            if job.state == "running":
                # A running handler may already have applied an effect. Record the
                # request; the run decides between cancelled and a real outcome.
                job.cancel_requested = True
                decision = "cancel_requested"
            else:
                transition(session, job, "cancelled")
                job.lease_owner = None
                job.lease_expires_at = None
                decision = "cancelled"
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="client" if principal is not None else "owner",
                actor_id=principal.client_id if principal is not None else None,
                event="job_cancel",
                account_id=job.account_id,
                decision=decision,
            )
            session.commit()
            return {"job_id": job.id, "state": job.state, "decision": decision}

    # -- scheduling and execution -----------------------------------------

    def _reschedule_next(self, session: Session, job: Job, reference: datetime) -> None:
        assert job.timezone and job.local_time
        job.run_at = next_local_occurrence(reference, job.timezone, job.local_time)
        job.state = "scheduled"

    def claim(self, worker_id: str, *, limit: int = 10) -> list[Job]:
        """Claim due work with a bounded lease. Returns detached snapshots."""
        if self._closed:
            return []
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid claim limit")
        created = self.clock()
        claimed: list[Job] = []
        with self.sessions() as session:
            candidates = session.scalars(
                select(Job.id)
                .where(
                    Job.state.in_(list(CLAIMABLE_STATES)),
                    Job.paused.is_(False),
                    Job.run_at <= created,
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < created),
                )
                .order_by(Job.run_at, Job.created_at, Job.id)
                .limit(limit)
            ).all()
            for job_id in candidates:
                pending = session.get(Job, job_id)
                if pending is None:  # pragma: no cover - deleted concurrently
                    continue
                validate_transition(pending.state, "running")
                result = session.execute(
                    update(Job)
                    .where(
                        Job.id == job_id,
                        Job.state.in_(list(CLAIMABLE_STATES)),
                        Job.paused.is_(False),
                        or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < created),
                    )
                    .values(
                        state="running",
                        lease_owner=worker_id,
                        lease_expires_at=created + timedelta(seconds=self.lease_seconds),
                        attempts=Job.attempts + 1,
                        updated_at=created,
                    )
                )
                if result.rowcount != 1:
                    continue
                job = session.get(Job, job_id)
                if job is None:  # pragma: no cover - deleted concurrently
                    continue
                session.add(
                    JobRun(
                        id=str(uuid.uuid4()),
                        job_id=job.id,
                        attempt=job.attempts,
                        state="running",
                        worker_id=worker_id,
                        started_at=created,
                    )
                )
                claimed.append(job)
            session.commit()
            return [self._snapshot(job) for job in claimed]

    @staticmethod
    def _snapshot(job: Job) -> Job:
        """Detached copy so handler awaits cannot keep the ORM session alive."""
        clone = Job()
        for column in Job.__table__.columns:
            setattr(clone, column.key, getattr(job, column.key))
        return clone

    async def run_once(self, worker_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        results = []
        for job in self.claim(worker_id, limit=limit):
            results.append(await self.execute_claim(job, worker_id))
        return results

    async def execute_claim(self, job: Job, worker_id: str) -> dict[str, Any]:
        kind = self._kinds.get(job.kind)
        started = self.clock()
        if kind is None:
            return self._finalize(job, JobOutcome(state="failed", error="unknown_job_kind"))
        context = JobContext(
            job_id=job.id,
            kind=job.kind,
            attempt=job.attempts,
            input=dict(job.input),
            client_id=job.client_id,
            credential_id=job.credential_id,
            account_id=job.account_id,
            correlation_id=job.correlation_id,
            worker_id=worker_id,
            cancel_requested=lambda: self._cancel_requested(job.id),
        )
        if job.cancel_requested or context.cancel_requested():
            return self._finalize(job, JobOutcome(state="cancelled", error="cancel_requested"))
        deadline = _as_aware_utc(job.deadline)
        if deadline is not None and started >= deadline:
            return self._finalize(job, JobOutcome(state="expired", error="deadline_exceeded"))
        try:
            outcome = await asyncio.wait_for(
                kind.handler(context), timeout=self.handler_timeout_seconds
            )
            if not isinstance(outcome, JobOutcome):
                raise TypeError("invalid handler outcome")
        except TimeoutError:
            outcome = JobOutcome(
                state="outcome_unknown" if kind.mutating else "failed", error="timeout"
            )
        except CancelledNoEffect:
            outcome = JobOutcome(state="cancelled", error="cancel_requested")
        except RateLimited as exc:
            delay = getattr(exc, "retry_after", None)
            if (
                getattr(exc, "outcome_unknown", True) is False
                and type(delay) in (int, float)
                and delay >= 0
                and job.attempts < job.max_attempts
            ):
                outcome = JobOutcome(
                    state="retry_wait", error="rate_limited", retry_after=float(delay)
                )
            else:
                outcome = JobOutcome(state="failed", error="rate_limited")
        except AdapterError as exc:
            code = normalized_error_code(getattr(exc, "code", None))
            unknown = getattr(exc, "outcome_unknown", True)
            if unknown is not False:
                outcome = JobOutcome(
                    state="outcome_unknown" if kind.mutating else "failed", error=code
                )
            else:
                outcome = JobOutcome(state="failed", error=code)
        except asyncio.CancelledError:
            # Shutdown or supervisor cancellation. A mutating handler may already
            # have applied an effect and its result is unknowable here, so record
            # that honestly instead of leaving the job claimable on lease expiry.
            try:
                self._finalize(
                    job,
                    JobOutcome(
                        state="outcome_unknown" if kind.mutating else "cancelled",
                        error="interrupted" if kind.mutating else "cancelled",
                    ),
                )
            finally:
                raise
        except JobError as exc:
            outcome = JobOutcome(state="failed", error=exc.code)
        except Exception:
            # Never surface upstream text. An ambiguous mutation stays unknown.
            outcome = JobOutcome(
                state="outcome_unknown" if kind.mutating else "failed", error="job_failure"
            )
        return self._finalize(job, outcome)

    def _cancel_requested(self, job_id: str) -> bool:
        with self.sessions() as session:
            job = session.get(Job, job_id)
            return bool(job and job.cancel_requested)

    def _finalize(self, job: Job, outcome: JobOutcome) -> dict[str, Any]:
        finished = self.clock()
        with self.sessions() as session:
            current = session.get(Job, job.id)
            if (
                current is None
                or current.state != "running"
                or current.lease_owner != job.lease_owner
            ):
                # Lost the lease or cancelled while running: do not overwrite.
                return {"job_id": job.id, "state": "lost_lease"}
            target = outcome.state
            error = outcome.error
            if target == "retry_wait":
                delay = outcome.retry_after if outcome.retry_after is not None else 30.0
                deadline = _as_aware_utc(current.deadline)
                if (
                    current.attempts >= current.max_attempts
                    or (deadline is not None and finished + timedelta(seconds=delay) > deadline)
                ):
                    target, error = "failed", error or "retry_exhausted"
                else:
                    current.run_at = finished + timedelta(seconds=float(delay))
                    current.state = "retry_wait"
                    current.lease_owner = None
                    current.lease_expires_at = None
                    current.last_error = error
                    current.cancel_requested = False
                    session.query(JobRun).filter(
                        JobRun.job_id == current.id, JobRun.attempt == current.attempts
                    ).update(
                        {
                            "state": "retry_wait",
                            "finished_at": finished,
                            "error_code": error,
                        }
                    )
                    audit.record(
                        session,
                        request_id=str(uuid.uuid4()),
                        actor_kind="scheduler",
                        actor_id=None,
                        event="job_retry",
                        account_id=current.account_id,
                        decision="retry_wait",
                        error_code=error,
                    )
                    session.commit()
                    return {"job_id": current.id, "state": "retry_wait", "retry_after": delay}
            try:
                validate_transition(current.state, target)
            except ValueError:
                target, error = "failed", "invalid_transition"
            current.state = target
            current.last_error = error
            current.result = outcome.result
            current.lease_owner = None
            current.lease_expires_at = None
            current.cancel_requested = False
            unknown = target == "outcome_unknown"
            session.query(JobRun).filter(
                JobRun.job_id == current.id, JobRun.attempt == current.attempts
            ).update(
                {
                    "state": target,
                    "finished_at": finished,
                    "error_code": error,
                    "outcome_unknown": unknown,
                }
            )
            recurring = current.schedule_kind == "daily_local"
            if target in {"succeeded", "failed"} and recurring:
                self._reschedule_next(session, current, finished)
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="scheduler",
                actor_id=None,
                event="job_finished",
                account_id=current.account_id,
                decision=target,
                error_code=error,
            )
            session.commit()
            return {
                "job_id": current.id,
                "state": target,
                "error": error,
                "next_run_at": (
                    _as_aware_utc(current.run_at).isoformat() if recurring else None
                ),
            }

    # -- recovery ----------------------------------------------------------

    def recover_expired_leases(self) -> list[dict[str, Any]]:
        """Resolve work whose owner died. Mutations may have taken effect."""
        created = self.clock()
        recovered: list[dict[str, Any]] = []
        with self.sessions() as session:
            stale = session.scalars(
                select(Job).where(
                    Job.state == "running",
                    or_(Job.lease_expires_at.is_(None), Job.lease_expires_at < created),
                )
            ).all()
            for job in stale:
                kind = self._kinds.get(job.kind)
                mutating = True if kind is None else kind.mutating
                if mutating:
                    target, error = "outcome_unknown", "lease_expired"
                elif job.attempts < job.max_attempts:
                    target, error = "retry_wait", "lease_expired"
                    job.run_at = created + timedelta(seconds=30)
                else:
                    target, error = "failed", "lease_expired"
                job.state = target
                job.last_error = error
                job.lease_owner = None
                job.lease_expires_at = None
                session.query(JobRun).filter(
                    JobRun.job_id == job.id, JobRun.attempt == job.attempts
                ).update(
                    {
                        "state": target,
                        "finished_at": created,
                        "error_code": error,
                        "outcome_unknown": target == "outcome_unknown",
                    }
                )
                audit.record(
                    session,
                    request_id=str(uuid.uuid4()),
                    actor_kind="scheduler",
                    actor_id=None,
                    event="job_recovery",
                    account_id=job.account_id,
                    decision=target,
                    error_code=error,
                )
                recovered.append({"job_id": job.id, "state": target, "error": error})
            session.commit()
        return recovered

    # -- misfire handling --------------------------------------------------

    async def tick(self, worker_id: str, *, limit: int = 10) -> dict[str, Any]:
        """One scheduler pass: apply misfire policy, then claim and execute."""
        if self._closed:
            return {"skipped": [], "runs": []}
        skipped = self.skip_misfired(worker_id)
        runs = await self.run_once(worker_id, limit=limit)
        return {"skipped": skipped, "runs": runs}

    def apply_misfire_policy(self, job: Job) -> str:
        """Return the action for an overdue job: ``expired``, ``reschedule`` or ``run``."""
        created = self.clock()
        run_at = _as_aware_utc(job.run_at)
        if run_at is None:
            return "expired"
        overdue = (created - run_at).total_seconds()
        if overdue <= job.misfire_grace_seconds:
            return "run"
        if job.misfire_policy == "run_once":
            return "run"
        return "reschedule" if job.schedule_kind == "daily_local" else "expired"

    def skip_misfired(self, worker_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
        """Apply misfire policy for overdue jobs before claiming. Bounded catch-up."""
        created = self.clock()
        handled: list[dict[str, Any]] = []
        with self.sessions() as session:
            overdue = session.scalars(
                select(Job)
                .where(
                    Job.state.in_(["queued", "scheduled", "retry_wait"]),
                    Job.paused.is_(False),
                    Job.run_at < created,
                )
                .order_by(Job.run_at)
                .limit(limit)
            ).all()
            for job in overdue:
                action = self.apply_misfire_policy(job)
                if action == "run":
                    continue
                if action == "expired":
                    if job.state not in {"queued", "scheduled", "retry_wait"}:
                        continue
                    job.state = "expired"
                    job.last_error = "misfire_skipped"
                else:
                    job.run_at = next_local_occurrence(
                        created, job.timezone, job.local_time
                    )
                    job.state = "scheduled"
                    job.last_error = "misfire_skipped"
                audit.record(
                    session,
                    request_id=str(uuid.uuid4()),
                    actor_kind="scheduler",
                    actor_id=None,
                    event="job_misfire",
                    account_id=job.account_id,
                    decision=action,
                )
                handled.append({"job_id": job.id, "action": action})
            session.commit()
        return handled
