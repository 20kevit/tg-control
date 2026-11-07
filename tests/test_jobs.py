"""Durable job and scheduler acceptance tests (synthetic fixtures only)."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from tg_control.adapters.base import ChatKind, RateLimited
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.auth import Principal, rotate_credential
from tg_control.core import Gateway
from tg_control.db import Account, Client, Credential, Grant, Job, JobRun
from tg_control.job_kinds import register_first_party_kinds
from tg_control.jobs import (
    JobContext,
    JobError,
    JobKind,
    JobOutcome,
    Jobs,
    Schedule,
    next_local_occurrence,
    validate_transition,
)
from tg_control.policy import Scope


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: int = 0
    mode: str | None = None


def make_account(session, alias="job account"):
    account = Account(alias=alias, phone_encrypted="synthetic-encrypted-phone")
    session.add(account)
    session.commit()
    return account


def make_client(session, name="job client"):
    client = Client(name=name)
    session.add(client)
    session.commit()
    rotate_credential(session, client.id)
    session.commit()
    principal = Principal(
        client_id=client.id,
        credential_id=session.scalar(
            select(Credential.id).where(
                Credential.client_id == client.id, Credential.revoked.is_(False)
            )
        ),
    )
    return client, principal


def grant(session, client, account, *, write=True):
    scope = Scope(enabled=True, allow_types=[ChatKind.PUBLIC_CHANNEL])
    session.add(
        Grant(
            client_id=client.id,
            account_id=account.id,
            read_scope=scope.model_dump(mode="json"),
            write_scope=scope.model_dump(mode="json") if write else Scope().model_dump(mode="json"),
            admin_scope=Scope().model_dump(mode="json"),
        )
    )
    session.commit()


class Clock:
    def __init__(self, value=None):
        self.value = value or datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.value

    def advance(self, **kwargs):
        self.value = self.value + timedelta(**kwargs)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def jobs(database, clock):
    service = Jobs(database, clock=clock, handler_timeout_seconds=1)
    calls = []

    async def handler(context: JobContext) -> JobOutcome:
        calls.append(context)
        if context.input.get("mode") == "unknown":
            return JobOutcome(state="outcome_unknown", error="unknown")
        if context.input.get("mode") == "rate_limited" and context.attempt == 1:
            raise RateLimited(60)
        if context.input.get("mode") == "orphan":
            await asyncio.sleep(10)
        return JobOutcome(state="succeeded", result={"value": context.input.get("value", 0)})

    service.register(
        JobKind(name="synthetic.echo", input_model=EchoInput, handler=handler, mutating=True)
    )
    service.calls = calls
    return service


async def enqueue(service, principal, clock, **overrides):
    fields = {
        "kind_name": "synthetic.echo",
        "payload": {"value": 1},
        "schedule": Schedule(kind="once", run_at=clock() - timedelta(seconds=1)),
        "deadline_seconds": 3600,
    }
    fields.update(overrides)
    return await service.enqueue(principal, **fields)


def test_state_machine_rejects_invalid_transitions():
    validate_transition("queued", "running")
    validate_transition("running", "outcome_unknown")
    with pytest.raises(ValueError):
        validate_transition("succeeded", "running")
    with pytest.raises(ValueError):
        validate_transition("queued", "succeeded")
    with pytest.raises(ValueError):
        validate_transition("nonexistent", "running")


def test_timezone_dst_skip_and_repeat():
    # Europe/Berlin springs forward on 2026-03-29 at 02:00 (02:30 does not exist).
    before = datetime(2026, 3, 28, 2, 0, tzinfo=timezone.utc)  # 03:00 CET on the 28th
    occurrence = next_local_occurrence(before, "Europe/Berlin", "02:30")
    assert occurrence == datetime(2026, 3, 30, 0, 30, tzinfo=timezone.utc)  # 02:30 CEST
    # Autumn fall-back on 2026-10-25 repeats 02:30; execute the first occurrence once.
    before_fall = datetime(2026, 10, 24, 23, 0, tzinfo=timezone.utc)  # 01:00 CEST
    first = next_local_occurrence(before_fall, "Europe/Berlin", "02:30")
    assert first == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert next_local_occurrence(first, "Europe/Berlin", "02:30") == datetime(
        2026, 10, 26, 1, 30, tzinfo=timezone.utc
    )
    with pytest.raises(JobError):
        next_local_occurrence(before, "Not/AZone", "02:30")
    with pytest.raises(JobError):
        next_local_occurrence(before, "UTC", "24:00")


def test_schedule_validation():
    with pytest.raises(ValueError):
        Schedule(kind="once")
    with pytest.raises(ValueError):
        Schedule(kind="once", run_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        Schedule(kind="daily_local", timezone="UTC")
    with pytest.raises(ValueError):
        Schedule(kind="once", run_at=datetime(2026, 1, 1, tzinfo=timezone.utc), timezone="UTC")


async def test_enqueue_requires_known_kind_valid_input_and_credentials(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    with pytest.raises(JobError) as unknown:
        await enqueue(jobs, principal, clock, kind_name="unknown.kind")
    assert unknown.value.code == "unknown_job_kind"
    with pytest.raises(JobError) as invalid:
        await enqueue(jobs, principal, clock, payload={"value": "not-an-int"})
    assert invalid.value.code == "invalid_input"
    created = await enqueue(jobs, principal, clock)
    assert created["state"] == "queued" and created["attempts"] == 0
    duplicate = str(uuid.uuid4())
    key = UUID(duplicate)
    await enqueue(jobs, principal, clock, idempotency_key=key)
    with pytest.raises(JobError) as conflict:
        await enqueue(jobs, principal, clock, idempotency_key=key)
    assert conflict.value.code == "duplicate_request"
    with database() as session:
        session.execute(
            update(Credential)
            .where(Credential.id == principal.credential_id)
            .values(revoked=True)
        )
        session.commit()
    with pytest.raises(JobError) as revoked:
        await enqueue(jobs, principal, clock)
    assert revoked.value.code == "credential_revoked"


async def test_due_job_runs_once_with_persisted_history(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    job = await enqueue(
        jobs,
        principal,
        clock,
        payload={"value": 7},
        schedule=Schedule(kind="once", run_at=clock() + timedelta(seconds=30)),
    )
    assert job["state"] == "scheduled"
    assert await jobs.run_once("worker-a") == []
    clock.advance(seconds=31)
    results = await jobs.run_once("worker-a")
    assert results == [
        {"job_id": job["job_id"], "state": "succeeded", "error": None, "next_run_at": None}
    ]
    inspected = jobs.inspect(principal, job["job_id"])
    assert inspected["state"] == "succeeded"
    assert inspected["result"] == {"value": 7}
    assert [run["state"] for run in inspected["runs"]] == ["succeeded"]
    # Terminal jobs are never executed or rewritten again.
    assert await jobs.run_once("worker-a") == []
    with pytest.raises(JobError):
        jobs.cancel(principal, job["job_id"])


async def test_retry_only_for_definite_failures(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    job = await enqueue(jobs, principal, clock, payload={"value": 2, "mode": "rate_limited"})
    clock.advance(seconds=5)
    first = await jobs.run_once("worker-a")
    assert first[0]["state"] == "retry_wait"
    inspected = jobs.inspect(principal, job["job_id"])
    assert inspected["state"] == "retry_wait" and inspected["attempts"] == 1
    assert await jobs.run_once("worker-a") == []
    clock.advance(seconds=61)
    second = await jobs.run_once("worker-a")
    assert second[0]["state"] == "succeeded"
    assert jobs.inspect(principal, job["job_id"])["attempts"] == 2


async def test_unknown_outcome_is_terminal_and_never_retried(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    job = await enqueue(jobs, principal, clock, payload={"mode": "unknown"})
    clock.advance(seconds=5)
    result = await jobs.run_once("worker-a")
    assert result[0]["state"] == "outcome_unknown"
    clock.advance(seconds=60)
    assert await jobs.run_once("worker-a") == []
    runs = jobs.inspect(principal, job["job_id"])["runs"]
    assert runs[0]["outcome_unknown"] is True
    assert runs[0]["error"] == "unknown"


async def test_handler_timeout_marks_mutations_unknown(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    job = await enqueue(jobs, principal, clock, payload={"mode": "orphan"})
    clock.advance(seconds=5)
    result = await jobs.run_once("worker-a")
    assert result[0]["state"] == "outcome_unknown"
    assert jobs.inspect(principal, job["job_id"])["last_error"] == "timeout"


async def test_pause_resume_cancel_and_deadline(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    job = await enqueue(jobs, principal, clock)
    jobs.set_paused(principal, job["job_id"], True)
    clock.advance(seconds=5)
    assert await jobs.run_once("worker-a") == []
    jobs.set_paused(principal, job["job_id"], False)
    cancelled = jobs.cancel(principal, job["job_id"])
    assert cancelled["decision"] == "cancelled"
    clock.advance(seconds=5)
    assert await jobs.run_once("worker-a") == []

    expired = await enqueue(jobs, principal, clock, deadline_seconds=30)
    clock.advance(seconds=120)
    result = await jobs.run_once("worker-a")
    assert result[0]["state"] == "expired"
    assert jobs.inspect(principal, expired["job_id"])["state"] == "expired"


async def test_cancellation_during_run_becomes_cancelled_only_without_effect(database, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    service = Jobs(database, clock=clock, handler_timeout_seconds=1)
    seen = []

    async def handler(context: JobContext) -> JobOutcome:
        # Simulate an owner cancelling while the handler is awaiting.
        with database() as session:
            session.execute(
                update(Job).where(Job.id == context.job_id).values(cancel_requested=True)
            )
            session.commit()
        context.check_cancelled()
        seen.append("effect")  # pragma: no cover - must not be reached
        return JobOutcome(state="succeeded")

    service.register(JobKind("synthetic.cancel", EchoInput, handler, mutating=True))
    job = await service.enqueue(
        principal,
        "synthetic.cancel",
        {"value": 1},
        schedule=Schedule(kind="once", run_at=clock() - timedelta(seconds=1)),
    )
    clock.advance(seconds=5)
    result = await service.run_once("worker-a")
    assert result[0]["state"] == "cancelled"
    assert seen == []
    assert service.inspect(principal, job["job_id"])["state"] == "cancelled"


async def test_expired_lease_recovery_is_conservative(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    job = await enqueue(jobs, principal, clock)
    clock.advance(seconds=5)
    claimed = jobs.claim("worker-a")
    assert len(claimed) == 1
    # The worker "crashes": its lease expires without a finalize.
    clock.advance(seconds=120)
    recovered = jobs.recover_expired_leases()
    assert recovered[0]["state"] == "outcome_unknown"
    assert jobs.inspect(principal, job["job_id"])["state"] == "outcome_unknown"
    assert jobs.recover_expired_leases() == []
    with database() as session:
        run = session.scalar(select(JobRun))
    assert run.state == "outcome_unknown" and run.outcome_unknown is True


async def test_non_mutating_lease_recovery_retries(database, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    service = Jobs(database, clock=clock, handler_timeout_seconds=1)
    never = []

    async def handler(context: JobContext) -> JobOutcome:
        never.append(context)  # pragma: no cover
        return JobOutcome(state="succeeded")

    service.register(JobKind("synthetic.read", EchoInput, handler, mutating=False))
    job = await service.enqueue(
        principal,
        "synthetic.read",
        {"value": 1},
        schedule=Schedule(kind="once", run_at=clock() - timedelta(seconds=1)),
    )
    clock.advance(seconds=5)
    service.claim("worker-a")
    clock.advance(seconds=120)
    assert service.recover_expired_leases()[0]["state"] == "retry_wait"
    clock.advance(seconds=31)
    assert (await service.run_once("worker-a"))[0]["state"] == "succeeded"
    assert service.inspect(principal, job["job_id"])["attempts"] == 2


async def test_misfire_policy_skip_and_run_once(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    once = await enqueue(jobs, principal, clock)
    clock.advance(hours=2)
    skipped = jobs.skip_misfired("worker-a")
    assert {"job_id": once["job_id"], "action": "expired"} in skipped
    assert jobs.inspect(principal, once["job_id"])["state"] == "expired"

    daily = await enqueue(
        jobs,
        principal,
        clock,
        schedule=Schedule(
            kind="daily_local",
            timezone="Europe/Berlin",
            local_time="09:00",
            misfire_policy="skip",
        ),
    )
    clock.advance(days=3)
    rescheduled = jobs.skip_misfired("worker-a")
    assert any(item["action"] == "reschedule" for item in rescheduled)
    inspected = jobs.inspect(principal, daily["job_id"])
    assert inspected["state"] == "scheduled"
    assert inspected["local_time"] == "09:00"

    catch_up = await enqueue(
        jobs,
        principal,
        clock,
        schedule=Schedule(
            kind="once",
            run_at=clock() - timedelta(hours=1),
            misfire_policy="run_once",
        ),
    )
    assert jobs.skip_misfired("worker-a") == []
    clock.advance(seconds=1)
    assert (await jobs.run_once("worker-a"))[0]["job_id"] == catch_up["job_id"]


async def test_recurring_daily_job_advances_after_success(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    job = await enqueue(
        jobs,
        principal,
        clock,
        schedule=Schedule(kind="daily_local", timezone="UTC", local_time="12:30"),
    )
    clock.advance(minutes=31)
    result = await jobs.run_once("worker-a")
    assert result[0]["state"] == "succeeded"
    inspected = jobs.inspect(principal, job["job_id"])
    assert inspected["state"] == "scheduled"
    assert inspected["run_at"] == "2026-11-08T12:30:00+00:00"
    assert inspected["runs"][0]["state"] == "succeeded"


async def test_jobs_are_isolated_per_client(database, jobs, clock):
    with database() as session:
        make_account(session)
        client_a, principal_a = make_client(session, name="client a")
        client_b, principal_b = make_client(session, name="client b")
    job = await enqueue(jobs, principal_a, clock)
    with pytest.raises(JobError) as hidden:
        jobs.inspect(principal_b, job["job_id"])
    assert hidden.value.code == "job_not_found"
    assert jobs.list_jobs(principal_b) == []
    assert [item["job_id"] for item in jobs.list_jobs(principal_a)] == [job["job_id"]]
    with pytest.raises(JobError):
        jobs.cancel(principal_b, job["job_id"])


async def test_registry_rejects_duplicates_and_invalid_kinds(jobs):
    with pytest.raises(ValueError):
        jobs.register(JobKind("synthetic.echo", EchoInput, jobs._kinds["synthetic.echo"].handler))
    with pytest.raises(ValueError):
        jobs.register(JobKind("bad name!", EchoInput, jobs._kinds["synthetic.echo"].handler))


async def test_scheduler_tick_and_graceful_close(database, jobs, clock):
    with database() as session:
        make_account(session)
        client, principal = make_client(session)
    await enqueue(jobs, principal, clock)
    clock.advance(seconds=5)
    result = await jobs.tick("worker-a")
    assert result["runs"][0]["state"] == "succeeded"
    jobs.close()
    with pytest.raises(JobError) as closed:
        await enqueue(jobs, principal, clock)
    assert closed.value.code == "scheduler_unavailable"
    assert jobs.claim("worker-a") == []


# --- integration with Gateway Core ---------------------------------------


async def test_delayed_send_reevaluates_authorization_at_execution(database, clock):
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter, sleep=lambda _delay: asyncio.sleep(0))
    service = Jobs(database, clock=clock, handler_timeout_seconds=2)
    register_first_party_kinds(service, gateway)
    with database() as session:
        account = make_account(session)
        client, principal = make_client(session)
        grant(session, client, account)
    schedule = Schedule(kind="once", run_at=clock() - timedelta(seconds=1))
    job = await service.enqueue(
        principal,
        "telegram.send",
        {"account_id": account.id, "chat_id": "public-channel", "text": "delayed synthetic"},
        schedule=schedule,
    )
    clock.advance(seconds=5)
    result = await service.run_once("worker-a")
    assert result[0]["state"] == "succeeded"
    data = await adapter.execute(account.id, "history", "public-channel", {"limit": 50})
    assert any(message["text"] == "delayed synthetic" for message in data["messages"])
    # Replay protection: the job's idempotency key already produced an effect.
    with database() as session:
        stored = session.get(Job, job["job_id"])
        assert stored.result.get("message_id") is not None
        assert session.scalar(select(JobRun)).state == "succeeded"


async def test_delayed_send_denied_when_policy_revoked_before_execution(database, clock):
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter, sleep=lambda _delay: asyncio.sleep(0))
    service = Jobs(database, clock=clock, handler_timeout_seconds=2)
    register_first_party_kinds(service, gateway)
    with database() as session:
        account = make_account(session)
        client, principal = make_client(session)
        grant(session, client, account)
    schedule = Schedule(kind="once", run_at=clock() - timedelta(seconds=1))
    job = await service.enqueue(
        principal,
        "telegram.send",
        {"account_id": account.id, "chat_id": "public-channel", "text": "denied later"},
        schedule=schedule,
    )
    with database() as session:
        grant_row = session.get(Grant, (client.id, account.id))
        grant_row.write_scope = Scope().model_dump(mode="json")
        session.commit()
    clock.advance(seconds=5)
    result = await service.run_once("worker-a")
    assert result[0]["state"] == "failed"
    assert service.inspect(None, job["job_id"])["last_error"] == "denied"
    history = await adapter.execute(account.id, "history", "public-channel", {"limit": 50})
    assert all(message["text"] != "denied later" for message in history["messages"])


async def test_delayed_send_blocked_when_credential_revoked(database, clock):
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter, sleep=lambda _delay: asyncio.sleep(0))
    service = Jobs(database, clock=clock, handler_timeout_seconds=2)
    register_first_party_kinds(service, gateway)
    with database() as session:
        account = make_account(session)
        client, principal = make_client(session)
        grant(session, client, account)
    schedule = Schedule(kind="once", run_at=clock() - timedelta(seconds=1))
    job = await service.enqueue(
        principal,
        "telegram.send",
        {"account_id": account.id, "chat_id": "public-channel", "text": "revoked"},
        schedule=schedule,
    )
    with database() as session:
        session.execute(
            update(Credential).where(Credential.id == principal.credential_id).values(revoked=True)
        )
        session.commit()
    clock.advance(seconds=5)
    result = await service.run_once("worker-a")
    assert result[0]["state"] == "failed"
    # The job's own credential is revoked, so the owner view is required here.
    assert service.inspect(None, job["job_id"])["last_error"] == "credential_revoked"
    history = await adapter.execute(account.id, "history", "public-channel", {"limit": 50})
    assert all(message["text"] != "revoked" for message in history["messages"])


async def test_unauthorized_delayed_send_is_rejected_at_creation(database, clock):
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter, sleep=lambda _delay: asyncio.sleep(0))
    service = Jobs(database, clock=clock)
    register_first_party_kinds(service, gateway)
    with database() as session:
        account = make_account(session)
        client, principal = make_client(session)
    with pytest.raises(JobError) as denied:
        await service.enqueue(
            principal,
            "telegram.send",
            {"account_id": account.id, "chat_id": "public-channel", "text": "never"},
            schedule=Schedule(kind="once", run_at=clock() - timedelta(seconds=1)),
        )
    assert denied.value.code == "denied"
    with database() as session:
        assert session.scalar(select(Job)) is None


async def test_job_inputs_cannot_carry_credentials_or_unknown_fields(database, clock):
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter)
    service = Jobs(database, clock=clock)
    register_first_party_kinds(service, gateway)
    with database() as session:
        account = make_account(session)
        client, principal = make_client(session)
        grant(session, client, account)
    with pytest.raises(JobError) as invalid:
        await service.enqueue(
            principal,
            "telegram.send",
            {
                "account_id": account.id,
                "chat_id": "public-channel",
                "text": "x",
                "credential": "tgc_secret",
            },
            schedule=Schedule(kind="once", run_at=clock() - timedelta(seconds=1)),
        )
    assert invalid.value.code == "invalid_input"
    with pytest.raises(JobError):
        await service.enqueue(
            principal,
            "telegram.send",
            {"account_id": account.id, "chat_id": "bad chat id", "text": "x"},
            schedule=Schedule(kind="once", run_at=clock() - timedelta(seconds=1)),
        )


async def test_gateway_execution_key_prevents_duplicate_delayed_effect(database, clock):
    adapter = SyntheticAdapter()
    gateway = Gateway(database, adapter, sleep=lambda _delay: asyncio.sleep(0))
    service = Jobs(database, clock=clock, handler_timeout_seconds=2)
    register_first_party_kinds(service, gateway)
    with database() as session:
        account = make_account(session)
        client, principal = make_client(session)
        grant(session, client, account)
    job = await service.enqueue(
        principal,
        "telegram.send",
        {"account_id": account.id, "chat_id": "public-channel", "text": "once only"},
        schedule=Schedule(kind="once", run_at=clock() - timedelta(seconds=1)),
    )
    clock.advance(seconds=5)
    await service.run_once("worker-a")
    # A second manual execution of the same job cannot duplicate the effect.
    with database() as session:
        stored = session.get(Job, job["job_id"])
        assert stored.state == "succeeded"
    history = await adapter.execute(account.id, "history", "public-channel", {"limit": 100})
    assert sum(1 for message in history["messages"] if message["text"] == "once only") == 1
