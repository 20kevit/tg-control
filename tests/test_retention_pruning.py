"""Retention pruning for job records and workflow runs (synthetic fixtures).

Pruning is bounded history deletion, not state management: unfinished work and
recent history must survive, a dry run must never write, and the deletion must
leave an audit trail.
"""


from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from tg_control.db import AuditEvent, Job, JobRun, Workflow, WorkflowRun, make_engine, make_sessions


def auth(token):
    return {"Authorization": "Bearer " + token}


def seed_jobs(settings):
    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    fresh = datetime.now(timezone.utc)
    identifiers = {}
    with sessions() as session:
        for name, state, updated in (
            ("old_success", "succeeded", old),
            ("old_failure", "failed", old),
            ("old_running", "running", old),
            ("old_queued", "queued", old),
            ("fresh_success", "succeeded", fresh),
        ):
            job = Job(
                kind="send_message",
                state=state,
                input={},
                run_at=old,
                created_at=updated,
                updated_at=updated,
            )
            session.add(job)
            session.flush()
            session.add(
                JobRun(
                    job_id=job.id,
                    attempt=1,
                    state=state,
                    started_at=updated,
                    finished_at=updated if state in {"succeeded", "failed"} else None,
                )
            )
            identifiers[name] = job.id
        session.commit()
    engine.dispose()
    return identifiers


def seed_workflows(settings):
    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)
    old = datetime.now(timezone.utc) - timedelta(days=30)
    fresh = datetime.now(timezone.utc)
    with sessions() as session:
        client = session.scalars(select(Workflow.__table__.c.id)).all()  # noqa: F841
        from tg_control.db import Client

        owner_client = Client(name="retention client")
        session.add(owner_client)
        session.flush()
        workflow = Workflow(
            name="retention workflow",
            definition={"actions": []},
            client_id=owner_client.id,
            created_at=old,
            updated_at=old,
        )
        session.add(workflow)
        session.flush()
        identifiers = {}
        for name, state, started in (
            ("old_succeeded", "succeeded", old),
            ("old_outcome_unknown", "outcome_unknown", old),
            ("old_running", "running", old),
            ("fresh_succeeded", "succeeded", fresh),
        ):
            run = WorkflowRun(
                workflow_id=workflow.id,
                workflow_version=1,
                state=state,
                trigger="manual",
                started_at=started,
                finished_at=started if state != "running" else None,
                steps=[],
                definition_snapshot={"actions": []},
            )
            session.add(run)
            session.flush()
            identifiers[name] = run.id
        session.commit()
    engine.dispose()
    return identifiers


def test_job_pruning_is_bounded_dry_run_first_and_audited(api, settings):
    identifiers = seed_jobs(settings)
    owner = auth(settings.owner_token)

    dry = api.post(
        "/v1/admin/maintenance/prune-jobs",
        headers=owner,
        json={"older_than_days": 7, "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    body = dry.json()
    assert body["dry_run"] is True
    assert body["eligible"] == 2 and body["deleted"] == 0
    assert body["retention_days"] == 7

    applied = api.post(
        "/v1/admin/maintenance/prune-jobs",
        headers=owner,
        json={"older_than_days": 7},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["deleted"] == 2

    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)
    with sessions() as session:
        remaining = {row.id for row in session.scalars(select(Job)).all()}
        run_jobs = {row.job_id for row in session.scalars(select(JobRun)).all()}
        events = [row.event for row in session.scalars(select(AuditEvent)).all()]
    engine.dispose()

    assert remaining == {
        identifiers["old_running"],
        identifiers["old_queued"],
        identifiers["fresh_success"],
    }
    assert identifiers["old_success"] not in run_jobs
    assert identifiers["old_failure"] not in run_jobs
    assert "jobs_pruned" in events

    again = api.post(
        "/v1/admin/maintenance/prune-jobs",
        headers=owner,
        json={"older_than_days": 7},
    )
    assert again.status_code == 200 and again.json()["deleted"] == 0


def test_workflow_run_pruning_never_touches_unfinished_runs(api, settings):
    identifiers = seed_workflows(settings)
    owner = auth(settings.owner_token)

    dry = api.post(
        "/v1/admin/maintenance/prune-workflows",
        headers=owner,
        json={"older_than_days": 7, "dry_run": True},
    )
    assert dry.status_code == 200, dry.text
    assert dry.json()["eligible"] == 2 and dry.json()["deleted"] == 0

    applied = api.post(
        "/v1/admin/maintenance/prune-workflows",
        headers=owner,
        json={"older_than_days": 7},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["deleted"] == 2

    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)
    with sessions() as session:
        remaining = {row.id for row in session.scalars(select(WorkflowRun)).all()}
        events = [row.event for row in session.scalars(select(AuditEvent)).all()]
    engine.dispose()
    assert remaining == {identifiers["old_running"], identifiers["fresh_succeeded"]}
    assert "workflow_runs_pruned" in events


def test_pruning_requires_owner_and_bounded_request(api, settings):
    created = api.post(
        "/v1/admin/clients", headers=auth(settings.owner_token), json={"name": "pruner"}
    )
    assert created.status_code == 201, created.text
    token = created.json()["credential"]

    assert (
        api.post("/v1/admin/maintenance/prune-jobs", headers=auth(token), json={}).status_code
        == 401
    )
    assert (
        api.post(
            "/v1/admin/maintenance/prune-jobs",
            headers=auth(settings.owner_token),
            json={"older_than_days": 0},
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/v1/admin/maintenance/prune-jobs",
            headers=auth(settings.owner_token),
            json={"older_than_days": 7, "dry_run": "yes"},
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/v1/admin/maintenance/prune-workflows",
            headers=auth(settings.owner_token),
            json={"older_than_days": 40000},
        ).status_code
        == 422
    )
