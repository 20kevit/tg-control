"""Opt-in automatic retention pruning: scheduling, safety and auditability.

The scheduled path must never widen what is pruned: it reuses the documented
bounded prune routines, stays disabled unless an owner enables it, runs at most
once per interval, only while the instance guard is held, and records exactly one
metadata-only audit row per run.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from test_api import auth
from test_api import create_resources as _create_resources

from tg_control.db import (
    AuditEvent,
    Job,
    Workflow,
    WorkflowRun,
    make_engine,
    make_sessions,
)
from tg_control.retention import AutoPrunePolicy, RetentionRunner


def owner_headers(settings):
    return {"Authorization": "Bearer " + settings.owner_token}


class FakeStep:
    def __init__(self, value=0, error=None):
        self.value = value
        self.error = error
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.error:
            raise self.error
        return self.value

    # The three prune collaborators expose different method names; the fake
    # records calls through any of them.
    def prune(self, *args, **kwargs):
        return self(*args, **kwargs)

    def prune_terminal(self, *args, **kwargs):
        return self(*args, **kwargs)

    def prune_runs(self, *args, **kwargs):
        return self(*args, **kwargs)


class FakeAudit:
    def __init__(self, deleted=0, error=None):
        self.deleted = deleted
        self.error = error
        self.records = []

    def prune(self, session, days):
        if self.error:
            raise self.error
        return self.deleted

    def record(self, session, **kwargs):
        self.records.append(kwargs)


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def commit(self):
        return None


def build_runner(
    *, clock, policy, guard=lambda: True, audit=None, jobs=None, workflows=None, events=None
):
    return RetentionRunner(
        sessions=lambda: FakeSession(),
        audit_module=audit or FakeAudit(),
        jobs=jobs or FakeStep({"deleted": 0}),
        workflows=workflows or FakeStep({"deleted": 0}),
        events=events or FakeStep(0),
        load_policy=lambda: policy,
        clock=clock,
        guard=guard,
    )


def test_policy_defaults_are_disabled_and_bounded():
    default = AutoPrunePolicy()
    assert default.enabled is False
    assert 60 <= default.interval_minutes <= 10_080
    for kwargs in (
        {"interval_minutes": 59},
        {"interval_minutes": 10_081},
        {"interval_minutes": 0},
        {"audit_retention_days": 0},
        {"event_retention_days": 0},
        {"history_retention_days": -1},
        {"enabled": "yes"},
        {"interval_minutes": True},
    ):
        with pytest.raises(ValueError):
            AutoPrunePolicy(**kwargs)


def test_disabled_policy_never_runs_and_never_prunes():
    audit = FakeAudit()
    jobs = FakeStep({"deleted": 3})
    runner = build_runner(
        clock=lambda: 10_000_000.0,
        policy=AutoPrunePolicy(enabled=False),
        audit=audit,
        jobs=jobs,
    )
    assert runner.is_due() is False
    assert asyncio.run(runner.run_if_due()) is None
    assert jobs.calls == [] and audit.records == []


def test_runs_once_per_interval_and_only_while_the_guard_is_held():
    clock = {"now": 0.0}
    audit = FakeAudit(deleted=2)
    jobs = FakeStep({"deleted": 1})
    workflows = FakeStep({"deleted": 4})
    events = FakeStep(5)
    policy = AutoPrunePolicy(enabled=True, interval_minutes=60)
    runner = build_runner(
        clock=lambda: clock["now"],
        policy=policy,
        audit=audit,
        jobs=jobs,
        workflows=workflows,
        events=events,
    )
    # A fresh process waits a full interval before its first automatic run.
    assert runner.is_due() is False
    clock["now"] = 60 * 60 - 1
    assert runner.is_due() is False
    clock["now"] = 60 * 60
    result = asyncio.run(runner.run_if_due())
    assert result["outcome"] == "success"
    assert result["deleted"] == {"audit": 2, "events": 5, "jobs": 1, "workflows": 4}
    assert audit.records and audit.records[0]["event"] == "retention_auto_prune"
    assert audit.records[0]["decision"] == "allow"
    # Not due again until a full interval has elapsed.
    clock["now"] += 60 * 59
    assert asyncio.run(runner.run_if_due()) is None
    clock["now"] += 60
    assert asyncio.run(runner.run_if_due()) is not None
    # A lost guard suspends automatic pruning entirely.
    lost = build_runner(
        clock=lambda: clock["now"] + 10**6,
        policy=policy,
        guard=lambda: False,
        audit=audit,
        jobs=jobs,
    )
    assert lost.is_due() is False
    assert asyncio.run(lost.run_if_due()) is None


def test_a_failing_step_does_not_block_the_others_and_is_audited():
    audit = FakeAudit(deleted=0)
    jobs = FakeStep({"deleted": 7})
    workflows = FakeStep(error=RuntimeError("boom"))
    events = FakeStep(error=RuntimeError("boom"))
    runner = build_runner(
        clock=lambda: 0.0,
        policy=AutoPrunePolicy(enabled=True),
        audit=audit,
        jobs=jobs,
        workflows=workflows,
        events=events,
    )
    result = runner.run_once(AutoPrunePolicy(enabled=True))
    assert result["outcome"] == "failure"
    assert set(result["failed_steps"]) == {"events", "workflows"}
    assert result["deleted"]["jobs"] == 7  # a later step still ran
    assert audit.records[0]["decision"] == "deny"
    assert audit.records[0]["error_code"] == "retention_failed"
    # No content, identifiers or counts are written to audit.
    assert set(audit.records[0]) == {
        "request_id",
        "actor_kind",
        "actor_id",
        "event",
        "decision",
        "error_code",
    }


def _sessions_for(settings):
    return make_sessions(make_engine(settings.database_url))


def _insert_history(sessions, client_id):
    """One old terminal job, one old queued job, one old finished run."""
    old = datetime.now(timezone.utc) - timedelta(days=200)
    terminal_id, queued_id, run_id = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    workflow_id = str(uuid.uuid4())
    with sessions() as session:
        session.add(
            Workflow(
                id=workflow_id,
                name="synthetic retention workflow",
                version=1,
                enabled=True,
                definition={},
                client_id=client_id,
            )
        )
        session.add(
            Job(
                id=terminal_id,
                kind="telegram.send",
                input={},
                state="succeeded",
                schedule_kind="once",
                run_at=old,
                created_at=old,
                updated_at=old,
            )
        )
        session.add(
            Job(
                id=queued_id,
                kind="telegram.send",
                input={},
                state="queued",
                schedule_kind="once",
                run_at=old,
                created_at=old,
                updated_at=old,
            )
        )
        session.add(
            WorkflowRun(
                id=run_id,
                workflow_id=workflow_id,
                workflow_version=1,
                state="succeeded",
                trigger="manual",
                started_at=old,
                finished_at=old,
                steps=[],
                definition_snapshot={},
            )
        )
        session.add(
            AuditEvent(
                request_id="synthetic-old",
                actor_kind="system",
                event="synthetic_old_event",
                decision="success",
                timestamp=old,
            )
        )
        session.commit()
    return terminal_id, queued_id, run_id


def test_api_auto_prune_deletes_only_eligible_rows_and_audits(api, settings):
    sessions = _sessions_for(settings)
    _, _, client = _create_resources(api, settings)
    terminal_id, queued_id, run_id = _insert_history(sessions, client["id"])
    runner = api.app.state.retention
    result = runner.run_once(
        AutoPrunePolicy(
            enabled=True,
            interval_minutes=60,
            audit_retention_days=90,
            event_retention_days=30,
            history_retention_days=90,
        )
    )
    assert result["outcome"] == "success"
    assert result["deleted"]["jobs"] == 1
    assert result["deleted"]["workflows"] == 1
    assert result["deleted"]["audit"] >= 1
    with sessions() as session:
        assert session.get(Job, terminal_id) is None
        assert session.get(Job, queued_id) is not None  # operational state kept
        assert session.get(WorkflowRun, run_id) is None
        events = [
            row.event
            for row in session.query(AuditEvent)
            .filter(AuditEvent.event == "retention_auto_prune")
            .all()
        ]
        assert events == ["retention_auto_prune"]
        remaining = session.query(AuditEvent).filter(
            AuditEvent.event == "synthetic_old_event"
        ).count()
        assert remaining == 0


def test_owner_enables_auto_prune_and_diagnostics_report_it(api, settings):
    owner = owner_headers(settings)
    default = api.get("/v1/admin/settings", headers=owner).json()
    assert default["auto_prune"] is False
    assert default["auto_prune_interval_minutes"] == 1440

    updated = api.put(
        "/v1/admin/settings",
        headers=owner,
        json={"auto_prune": True, "auto_prune_interval_minutes": 60, "history_retention_days": 30},
    )
    assert updated.status_code == 200
    stored = api.get("/v1/admin/settings", headers=owner).json()
    assert stored["auto_prune"] is True
    assert stored["auto_prune_interval_minutes"] == 60
    assert stored["history_retention_days"] == 30

    diagnostics = api.get("/v1/admin/diagnostics", headers=owner).json()
    assert diagnostics["retention"]["auto_prune"]["state"] == "enabled"
    assert diagnostics["retention"]["auto_prune"]["interval_minutes"] == 60
    # Diagnostic snapshot stays metadata-only.
    assert "deleted" not in diagnostics["retention"]["auto_prune"]

    too_fast = api.put(
        "/v1/admin/settings", headers=owner, json={"auto_prune_interval_minutes": 59}
    )
    assert too_fast.status_code == 422
    auto_prune = api.app.state.retention
    assert isinstance(auto_prune, RetentionRunner)
    # Guard/capability sanity: a client credential can never reach this control.
    _, _, client = _create_resources(api, settings)
    client_headers = auth(client["credential"])
    assert api.get("/v1/admin/settings", headers=client_headers).status_code == 401
