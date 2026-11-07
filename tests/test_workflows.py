"""Workflow definition and bounded execution acceptance tests (synthetic)."""

import asyncio
import uuid

import pytest
from sqlalchemy import select, update

from tg_control.adapters.base import ChatKind
from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.auth import Principal, rotate_credential
from tg_control.core import Gateway
from tg_control.db import Account, Client, Credential, Grant, WorkflowRun
from tg_control.policy import Scope
from tg_control.workflows import (
    RunRequest,
    WorkflowError,
    WorkflowInput,
    Workflows,
)


class Env:
    def __init__(self, database):
        self.database = database
        self.adapter = SyntheticAdapter()
        self.gateway = Gateway(database, self.adapter, sleep=lambda _delay: asyncio.sleep(0))
        self.service = Workflows(database, self.gateway)
        with database() as session:
            self.account = Account(alias="workflow account", phone_encrypted="synthetic")
            self.client = Client(name="workflow client")
            session.add_all([self.account, self.client])
            session.commit()
            rotate_credential(session, self.client.id)
            session.commit()
            self.principal = Principal(
                client_id=self.client.id,
                credential_id=session.scalar(
                    select(Credential.id).where(
                        Credential.client_id == self.client.id,
                        Credential.revoked.is_(False),
                    )
                ),
            )
        self.grant()

    def grant(self, enabled=True):
        scope = Scope(enabled=enabled, allow_types=[ChatKind.PUBLIC_CHANNEL])
        with self.database() as session:
            existing = session.get(Grant, (self.client.id, self.account.id))
            values = {
                "read_scope": scope.model_dump(mode="json"),
                "write_scope": scope.model_dump(mode="json"),
                "admin_scope": Scope().model_dump(mode="json"),
            }
            if existing is None:
                session.add(
                    Grant(client_id=self.client.id, account_id=self.account.id, **values)
                )
            else:
                for key, value in values.items():
                    setattr(existing, key, value)
            session.commit()

    def definition(self, actions=None, **overrides):
        fields = {
            "name": "synthetic workflow",
            "client_id": self.client.id,
            "actions": [
                {
                    "kind": "send",
                    "account_id": self.account.id,
                    "chat_id": "public-channel",
                    "text": "workflow step one",
                }
            ]
            if actions is None
            else actions,
        }
        fields.update(overrides)
        return WorkflowInput(**fields)

    async def history(self, chat="public-channel"):
        data = await self.adapter.execute(
            str(self.account.id), "history", chat, {"limit": 100}
        )
        return data["messages"]


@pytest.fixture
def env(database):
    return Env(database)


def test_definition_validation_rejects_unsafe_or_unbounded_shapes(env):
    with pytest.raises(ValueError):
        env.definition(actions=[])
    with pytest.raises(ValueError):
        env.definition(
            actions=[
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "x",
                }
            ]
            * 11
        )
    with pytest.raises(ValueError):
        env.definition(
            actions=[
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "x" * 5000,
                }
            ]
        )
    with pytest.raises(ValueError):
        env.definition(trigger="event")
    with pytest.raises(ValueError):
        env.definition(
            actions=[
                {
                    "kind": "shell",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "command": "rm -rf /",
                }
            ]
        )
    with pytest.raises(ValueError):
        env.definition(timeout_seconds=0)
    with pytest.raises(ValueError):
        env.definition(
            conditions=[{"field": "parameter", "op": "eq", "value": "x"}],
        )
    with pytest.raises(ValueError):
        env.definition(conditions=[{"field": "chat_id", "op": "exists", "value": "x"}])


def test_create_requires_known_enabled_client(env):
    with pytest.raises(WorkflowError) as unknown:
        env.service.create(env.definition(client_id=uuid.uuid4()))
    assert unknown.value.code == "unknown_client"
    created = env.service.create(env.definition())
    assert created["version"] == 1 and created["enabled"] is True
    with pytest.raises(WorkflowError) as duplicate:
        env.service.create(env.definition())
    assert duplicate.value.code == "duplicate_workflow"


async def test_manual_run_executes_actions_through_core(env):
    workflow = env.service.create(env.definition())
    run = await env.service.run(workflow["workflow_id"], RunRequest())
    assert run["state"] == "succeeded"
    assert [step["status"] for step in run["steps"]] == ["success"]
    texts = [message["text"] for message in await env.history()]
    assert "workflow step one" in texts


async def test_conditions_and_parameters_control_steps(env):
    definition = env.definition(
        conditions=[{"field": "parameter", "key": "mode", "op": "eq", "value": "go"}],
        actions=[
            {
                "kind": "send",
                "account_id": env.account.id,
                "chat_id": "public-channel",
                "text": "conditional step",
            }
        ],
    )
    workflow = env.service.create(definition)
    skipped = await env.service.run(
        workflow["workflow_id"], RunRequest(parameters={"mode": "stop"})
    )
    assert skipped["state"] == "succeeded" and skipped["steps"] == []
    assert all(message["text"] != "conditional step" for message in await env.history())
    allowed = await env.service.run(
        workflow["workflow_id"], RunRequest(parameters={"mode": "go"})
    )
    assert [step["status"] for step in allowed["steps"]] == ["success"]

    with_action_condition = env.service.create(
        env.definition(
            name="step condition",
            actions=[
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "guarded step",
                    "when": {"field": "parameter", "key": "flag", "op": "exists"},
                }
            ],
        )
    )
    no_flag = await env.service.run(with_action_condition["workflow_id"], RunRequest())
    assert no_flag["steps"] == [{"index": 0, "kind": "send", "status": "skipped"}]


async def test_dry_run_authorizes_without_effects(env):
    workflow = env.service.create(env.definition())
    run = await env.service.run(workflow["workflow_id"], RunRequest(dry_run=True))
    assert run["state"] == "succeeded"
    assert run["trigger"] == "dry_run"
    assert run["steps"][0]["status"] == "would_execute"
    assert all("workflow step one" != message["text"] for message in await env.history())


async def test_step_denial_stops_or_continues_per_policy(env):
    env.grant(enabled=False)
    stopping = env.service.create(
        env.definition(
            name="stop workflow",
            actions=[
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "denied one",
                },
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "denied two",
                },
            ],
        )
    )
    run = await env.service.run(stopping["workflow_id"], RunRequest())
    assert run["state"] == "partial_failure"
    assert [step["status"] for step in run["steps"]] == ["failure"]
    assert run["steps"][0]["error"] == "denied"
    assert run["steps"][0].get("outcome_unknown") is not True
    assert all("denied" not in message["text"] for message in await env.history())

    env.grant()
    continuing = env.service.create(
        env.definition(
            name="continue workflow",
            stop_on_error=False,
            actions=[
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "private-direct",
                    "text": "unauthorized chat",
                },
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "later step",
                },
            ],
        )
    )
    run = await env.service.run(continuing["workflow_id"], RunRequest())
    assert [step["status"] for step in run["steps"]] == ["failure", "success"]
    assert run["state"] == "partial_failure"
    assert "later step" in [message["text"] for message in await env.history()]


async def test_definition_versions_are_immutable_per_run(env):
    workflow = env.service.create(env.definition())
    first = await env.service.run(workflow["workflow_id"], RunRequest())
    updated = env.service.update(
        workflow["workflow_id"],
        env.definition(
            actions=[
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "second version",
                }
            ]
        ),
    )
    assert updated["version"] == 2
    stored_first = env.service.get_run(first["run_id"])
    assert stored_first["workflow_version"] == 1
    assert stored_first["steps"][0]["status"] == "success"
    with env.database() as session:
        run = session.get(WorkflowRun, first["run_id"])
        assert run.definition_snapshot["actions"][0]["text"] == "workflow step one"
    second = await env.service.run(workflow["workflow_id"], RunRequest())
    assert second["workflow_version"] == 2


async def test_disabled_workflow_and_unknown_ids(env):
    workflow = env.service.create(env.definition())
    env.service.set_enabled(workflow["workflow_id"], False)
    with pytest.raises(WorkflowError) as disabled:
        await env.service.run(workflow["workflow_id"], RunRequest())
    assert disabled.value.code == "workflow_disabled"
    with pytest.raises(WorkflowError):
        env.service.get(str(uuid.uuid4()))
    with pytest.raises(WorkflowError):
        env.service.get("not-a-uuid")
    with pytest.raises(WorkflowError):
        env.service.get_run(str(uuid.uuid4()))


async def test_step_rechecks_authorization_after_earlier_effect(env):
    workflow = env.service.create(
        env.definition(
            actions=[
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "first effect",
                },
                {
                    "kind": "send",
                    "account_id": env.account.id,
                    "chat_id": "public-channel",
                    "text": "second effect",
                },
            ]
        )
    )
    original = env.gateway.execute
    calls = {"count": 0}

    async def revoke_after_first(principal, batch, request_id=None):
        calls["count"] += 1
        if calls["count"] == 2:
            env.grant(enabled=False)
        return await original(principal, batch, request_id)

    env.gateway.execute = revoke_after_first
    run = await env.service.run(workflow["workflow_id"], RunRequest())
    assert [step["status"] for step in run["steps"]] == ["success", "failure"]
    assert run["steps"][1]["error"] == "denied"
    texts = [message["text"] for message in await env.history()]
    assert "first effect" in texts and "second effect" not in texts


async def test_interrupted_run_recovery_is_conservative(env):
    workflow = env.service.create(env.definition())
    with env.database() as session:
        session.add(
            WorkflowRun(
                id=str(uuid.uuid4()),
                workflow_id=workflow["workflow_id"],
                workflow_version=1,
                state="running",
                trigger="manual",
                started_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
                steps=[],
                definition_snapshot={},
            )
        )
        session.commit()
    recovered = env.service.recover_interrupted()
    assert recovered and recovered[0]["state"] == "outcome_unknown"
    assert env.service.recover_interrupted() == []


async def test_run_with_revoked_credential_does_not_execute(env):
    workflow = env.service.create(env.definition())
    with env.database() as session:
        session.execute(
            update(Credential)
            .where(Credential.id == env.principal.credential_id)
            .values(revoked=True)
        )
        session.commit()
    run = await env.service.run(workflow["workflow_id"], RunRequest())
    assert run["state"] == "failed" and run["error"] == "credential_revoked"
    assert all("workflow step one" != message["text"] for message in await env.history())


async def test_run_history_is_owner_visible_and_bounded(env):
    workflow = env.service.create(env.definition())
    first = await env.service.run(workflow["workflow_id"], RunRequest())
    second = await env.service.run(workflow["workflow_id"], RunRequest())
    runs = env.service.runs(workflow["workflow_id"])
    assert {run["run_id"] for run in runs} == {first["run_id"], second["run_id"]}
    assert env.service.get_run(first["run_id"])["state"] == "succeeded"
    with pytest.raises(WorkflowError):
        env.service.runs(workflow["workflow_id"], limit=0)


async def test_concurrent_runs_are_bounded_and_refused_without_side_effects(env):
    """A bounded in-flight limit refuses extra runs instead of queueing them."""
    definition = env.definition()
    workflow = env.service.create(definition)
    with pytest.raises(ValueError):
        Workflows(env.database, env.gateway, max_inflight=0)
    with pytest.raises(ValueError):
        Workflows(env.database, env.gateway, max_inflight=65)

    env.service.max_inflight = 1
    release = asyncio.Event()
    started = asyncio.Event()

    async def slow_run(workflow_id, request, *, request_id=None):
        started.set()
        await release.wait()
        return {"run_id": "synthetic", "state": "succeeded"}

    env.service._run = slow_run
    first = asyncio.create_task(env.service.run(workflow["workflow_id"], RunRequest()))
    await started.wait()
    with pytest.raises(WorkflowError) as busy:
        await env.service.run(workflow["workflow_id"], RunRequest())
    assert busy.value.code == "busy" and busy.value.status_code == 429
    release.set()
    assert (await first)["state"] == "succeeded"
    assert env.service._inflight == 0
    # The refusal created no run record, and capacity is available again.
    with env.database() as session:
        assert session.scalars(select(WorkflowRun)).all() == []
    env.service._run = env.service._run  # restored by the fixture on the next test
