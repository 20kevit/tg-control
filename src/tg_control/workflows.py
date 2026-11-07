"""Bounded, owner-configured workflow execution (synthetic-only).

Design notes (see ADR-010):
- Only manual triggers are supported. Event/message triggers are explicitly
  rejected: incoming Telegram content is never an instruction or authorization.
- A definition is a linear, bounded list of typed actions with optional typed
  conditions over caller-supplied run parameters. There is no ``eval``, shell,
  SQL, URL fetch or dynamic code path, and no action can reference another
  workflow, so cycles and self-triggering are rejected by construction.
- Every action executes through Gateway Core with an explicitly named client's
  principal, which revalidates credential, account and chat authorization
  immediately before the effect. A workflow is never a privileged principal.
- Each run snapshots the definition version immutably, persists per-step
  outcomes, and uses a deterministic per-step idempotency key so a repeated step
  cannot duplicate an effect. Interrupted runs are resolved conservatively at
  startup and are never resumed automatically.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import audit
from .core import BatchRejected, Gateway
from .db import (
    EXPECTED_SCHEMA_REVISION,  # noqa: F401  (documentation: workflows ship with the schema)
    Client,
    Workflow,
    WorkflowRun,
    new_id,
    now,
)
from .observability import record_component_outcome
from .policy import ChatId
from .schemas import Batch, Send

MAX_ACTIONS = 10
MAX_CONDITIONS = 8
MAX_PARAMETERS = 8
MAX_PARAMETER_LENGTH = 256
MAX_TEXT = 4096
MAX_RESULT_BYTES = 8192
MIN_TIMEOUT = 1
MAX_TIMEOUT = 120
CONDITION_FIELDS = frozenset({"chat_id", "account_id", "parameter", "trigger"})
CONDITION_OPS = frozenset({"eq", "ne", "in", "exists", "not_exists"})
TRIGGERS = frozenset({"manual"})


class WorkflowError(Exception):
    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    field: Literal["chat_id", "account_id", "parameter", "trigger"]
    key: Annotated[str, Field(min_length=1, max_length=64)] | None = None
    op: Literal["eq", "ne", "in", "exists", "not_exists"]
    value: Any | None = None

    @model_validator(mode="after")
    def consistent(self):
        if self.field == "parameter":
            if not self.key:
                raise ValueError("parameter conditions require a key")
        elif self.key is not None:
            raise ValueError("only parameter conditions accept a key")
        if self.op in {"exists", "not_exists"}:
            if self.value is not None:
                raise ValueError("existence conditions do not take a value")
        elif self.op == "in":
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 16:
                raise ValueError("in conditions require a bounded list")
            if any(not isinstance(item, str) or len(item) > 128 for item in self.value):
                raise ValueError("in conditions accept short strings")
        elif not isinstance(self.value, str) or len(self.value) > 128:
            raise ValueError("conditions accept short string values")
        return self

    def matches(self, context: dict[str, Any]) -> bool:
        if self.field == "parameter":
            present = self.key in context.get("parameters", {})
            actual = context.get("parameters", {}).get(self.key)
        else:
            present = self.field in context
            actual = context.get(self.field)
        if self.op == "exists":
            return present
        if self.op == "not_exists":
            return not present
        if self.op == "in":
            return isinstance(actual, str) and actual in (self.value or [])
        if self.op == "eq":
            return actual == self.value
        return actual != self.value


class SendAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["send"]
    when: Condition | None = None
    account_id: UUID
    chat_id: ChatId
    text: Annotated[str, Field(min_length=1, max_length=MAX_TEXT)]


Action = Annotated[SendAction, Field(discriminator="kind")]


class WorkflowInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: Annotated[str, Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9 _-]+$")]
    trigger: Literal["manual"] = "manual"
    conditions: Annotated[list[Condition], Field(max_length=MAX_CONDITIONS)] = Field(
        default_factory=list
    )
    actions: Annotated[list[Action], Field(min_length=1, max_length=MAX_ACTIONS)]
    client_id: UUID
    stop_on_error: bool = True
    timeout_seconds: Annotated[int, Field(strict=True, ge=MIN_TIMEOUT, le=MAX_TIMEOUT)] = 30
    enabled: bool = True

    @field_validator("trigger")
    @classmethod
    def manual_only(cls, value: str) -> str:
        if value not in TRIGGERS:
            # Event/message triggers are not approved behavior.
            raise ValueError("only manual triggers are approved")
        return value


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    parameters: Annotated[dict[str, str], Field(max_length=MAX_PARAMETERS)] = Field(
        default_factory=dict
    )
    dry_run: bool = False

    @field_validator("parameters")
    @classmethod
    def bounded(cls, value: dict[str, str]) -> dict[str, str]:
        for key, item in value.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 64:
                raise ValueError("invalid parameter name")
            if not isinstance(item, str) or len(item) > MAX_PARAMETER_LENGTH:
                raise ValueError("invalid parameter value")
        return value


def serialize_definition(workflow: Workflow) -> dict[str, Any]:
    return {
        "workflow_id": workflow.id,
        "name": workflow.name,
        "version": workflow.version,
        "enabled": workflow.enabled,
        "trigger": "manual",
        "definition": workflow.definition,
        "client_id": workflow.client_id,
        "created_at": workflow.created_at.isoformat() if workflow.created_at else None,
    }


def serialize_run(run: WorkflowRun) -> dict[str, Any]:
    return {
        "run_id": run.id,
        "workflow_id": run.workflow_id,
        "workflow_version": run.workflow_version,
        "state": run.state,
        "trigger": run.trigger,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        "error": run.error,
        "correlation_id": run.correlation_id,
        "steps": run.steps,
    }


PRUNEABLE_RUN_STATES = frozenset({"succeeded", "partial_failure", "failed", "outcome_unknown"})


class Workflows:
    def __init__(
        self,
        sessions: sessionmaker,
        gateway: Gateway,
        *,
        step_timeout_seconds: float = 30.0,
        max_inflight: int = 4,
    ) -> None:
        if not 0 < step_timeout_seconds <= 300:
            raise ValueError("invalid step timeout")
        if type(max_inflight) is not int or not 1 <= max_inflight <= 64:
            raise ValueError("invalid workflow concurrency limit")
        self.sessions = sessions
        self.gateway = gateway
        self.step_timeout_seconds = step_timeout_seconds
        self.max_inflight = max_inflight
        self._inflight = 0

    # -- definitions -------------------------------------------------------

    def create(self, data: WorkflowInput, *, request_id: str | None = None) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        with self.sessions() as session:
            client = session.get(Client, str(data.client_id))
            if client is None or not client.enabled:
                raise WorkflowError("unknown_client", 422)
            workflow = Workflow(
                id=new_id(),
                name=data.name,
                version=1,
                enabled=data.enabled,
                definition=self._definition_body(data),
                client_id=str(data.client_id),
                created_at=now(),
                updated_at=now(),
            )
            session.add(workflow)
            try:
                session.commit()
            except IntegrityError:
                session.rollback()
                raise WorkflowError("duplicate_workflow", 409) from None
            self._audit(session, "workflow_create", workflow, request_id, "success")
            session.commit()
            return serialize_definition(workflow)

    @staticmethod
    def _definition_body(data: WorkflowInput) -> dict[str, Any]:
        return {
            "trigger": "manual",
            "conditions": [condition.model_dump(mode="json") for condition in data.conditions],
            "actions": [action.model_dump(mode="json") for action in data.actions],
            "stop_on_error": data.stop_on_error,
            "timeout_seconds": data.timeout_seconds,
        }

    def update(
        self, workflow_id: str, data: WorkflowInput, *, request_id: str | None = None
    ) -> dict[str, Any]:
        """Create a new immutable version. Existing runs keep their snapshot."""
        request_id = request_id or str(uuid.uuid4())
        with self.sessions() as session:
            workflow = self._get(session, workflow_id)
            if workflow.name != data.name:
                raise WorkflowError("name_change_not_allowed", 422)
            if str(data.client_id) != workflow.client_id:
                raise WorkflowError("principal_change_not_allowed", 422)
            workflow.version += 1
            workflow.enabled = data.enabled
            workflow.definition = self._definition_body(data)
            workflow.updated_at = now()
            self._audit(session, "workflow_update", workflow, request_id, "success")
            session.commit()
            return serialize_definition(workflow)

    def set_enabled(
        self, workflow_id: str, enabled: bool, *, request_id: str | None = None
    ) -> dict[str, Any]:
        if type(enabled) is not bool:
            raise WorkflowError("invalid_request", 422)
        with self.sessions() as session:
            workflow = self._get(session, workflow_id)
            workflow.enabled = enabled
            workflow.updated_at = now()
            self._audit(
                session,
                "workflow_enable" if enabled else "workflow_disable",
                workflow,
                request_id or str(uuid.uuid4()),
                "success",
            )
            session.commit()
            return serialize_definition(workflow)

    def list(self) -> list[dict[str, Any]]:
        with self.sessions() as session:
            rows = session.scalars(
                select(Workflow).order_by(Workflow.created_at, Workflow.id)
            ).all()
            return [serialize_definition(row) for row in rows]

    def get(self, workflow_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            return serialize_definition(self._get(session, workflow_id))

    @staticmethod
    def _get(session: Session, workflow_id: str) -> Workflow:
        if not isinstance(workflow_id, str) or len(workflow_id) != 36:
            raise WorkflowError("workflow_not_found", 404)
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise WorkflowError("workflow_not_found", 404)
        return workflow

    @staticmethod
    def _audit(
        session: Session, event: str, workflow: Workflow, request_id: str, decision: str
    ) -> None:
        audit.record(
            session,
            request_id=request_id,
            actor_kind="owner",
            actor_id=None,
            event=event,
            account_id=None,
            subject_client_id=workflow.client_id,
            decision=decision,
        )

    # -- execution ---------------------------------------------------------

    @staticmethod
    def _validate_run_state(workflow: Workflow) -> dict[str, Any]:
        if not workflow.enabled:
            raise WorkflowError("workflow_disabled", 409)
        try:
            body = WorkflowInput.model_validate(
                {
                    "name": workflow.name,
                    "trigger": workflow.definition.get("trigger", "manual"),
                    "conditions": workflow.definition.get("conditions", []),
                    "actions": workflow.definition.get("actions", []),
                    "client_id": workflow.client_id,
                    "stop_on_error": workflow.definition.get("stop_on_error", True),
                    "timeout_seconds": workflow.definition.get("timeout_seconds", 30),
                    "enabled": workflow.enabled,
                }
            )
        except ValueError:
            raise WorkflowError("invalid_definition", 422) from None
        return {
            "conditions": body.conditions,
            "actions": body.actions,
            "stop_on_error": body.stop_on_error,
            "timeout_seconds": body.timeout_seconds,
            "client_id": body.client_id,
        }

    async def run(
        self,
        workflow_id: str,
        request: RunRequest,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Start a run, refusing rather than queueing beyond the in-flight bound."""
        if self._inflight >= self.max_inflight:
            raise WorkflowError("busy", 429)
        self._inflight += 1
        try:
            return await self._run(workflow_id, request, request_id=request_id)
        finally:
            self._inflight -= 1

    async def _run(
        self,
        workflow_id: str,
        request: RunRequest,
        *,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        request_id = request_id or str(uuid.uuid4())
        run_id = new_id()
        with self.sessions() as session:
            workflow = self._get(session, workflow_id)
            definition = self._validate_run_state(workflow)
            client_id = workflow.client_id
            version = workflow.version
            snapshot = dict(workflow.definition)
            client = session.get(Client, client_id)
            if client is None or not client.enabled:
                raise WorkflowError("unknown_client", 422)
            run = WorkflowRun(
                id=run_id,
                workflow_id=workflow.id,
                workflow_version=version,
                state="running",
                trigger="dry_run" if request.dry_run else "manual",
                started_at=now(),
                steps=[],
                error=None,
                correlation_id=request_id,
                definition_snapshot=snapshot,
            )
            session.add(run)
            self._audit(
                session,
                "workflow_run",
                workflow,
                request_id,
                "dry_run" if request.dry_run else "started",
            )
            session.commit()

        from .auth import active_principal

        with self.sessions() as session:
            principal = active_principal(session, client_id)
        if principal is None:
            return self._finish(run_id, "failed", [], "credential_revoked")

        context: dict[str, Any] = {
            "trigger": "dry_run" if request.dry_run else "manual",
            "parameters": dict(request.parameters),
        }
        steps: list[dict[str, Any]] = []

        async def execute() -> str:
            for condition in definition["conditions"]:
                if not condition.matches(context):
                    return "skipped_by_condition"
            failures = 0
            for index, action in enumerate(definition["actions"]):
                if action.when is not None and not action.when.matches(context):
                    steps.append(
                        {"index": index, "kind": action.kind, "status": "skipped"}
                    )
                    continue
                outcome = await self._run_action(
                    principal, action, run_id, index, request.dry_run, request_id
                )
                steps.append(outcome)
                if outcome["status"] in {"success", "would_execute", "skipped"}:
                    continue
                failures += 1
                if definition["stop_on_error"]:
                    return "stopped_on_error"
            return "partial_failure" if failures else "succeeded"

        try:
            state = await asyncio.wait_for(
                execute(), timeout=definition["timeout_seconds"]
            )
        except TimeoutError:
            state = "timeout"
        except asyncio.CancelledError:
            self._finish(run_id, "outcome_unknown", steps, "cancelled")
            raise
        if state in {"succeeded", "skipped_by_condition"}:
            return self._finish(run_id, "succeeded", steps, None, dry_run=request.dry_run)
        if state in {"stopped_on_error", "partial_failure"}:
            return self._finish(run_id, "partial_failure", steps, "step_failed")
        return self._finish(run_id, "outcome_unknown", steps, "timeout")

    async def _run_action(
        self,
        principal,
        action: SendAction,
        run_id: str,
        index: int,
        dry_run: bool,
        request_id: str,
    ) -> dict[str, Any]:
        if dry_run:
            # Dry-run performs authorization only; no adapter mutation is made.
            batch = Batch(
                actions=[self._batch_action(action)],
                idempotency_key=self._key(run_id, index),
            )
            try:
                results = await self.gateway.preauthorize(principal, batch, request_id)
            except BatchRejected as exc:
                return {
                    "index": index,
                    "kind": action.kind,
                    "status": "denied",
                    "error": "credential_revoked" if exc.status_code == 401 else "denied",
                }
            if not results[0]["allowed"]:
                return {
                    "index": index,
                    "kind": action.kind,
                    "status": "denied",
                    "error": results[0]["error"] or "denied",
                }
            return {"index": index, "kind": action.kind, "status": "would_execute"}
        batch = Batch(
            actions=[self._batch_action(action)],
            idempotency_key=self._key(run_id, index),
        )
        try:
            result = await asyncio.wait_for(
                self.gateway.execute(principal, batch, request_id),
                timeout=self.step_timeout_seconds,
            )
        except TimeoutError:
            return {
                "index": index,
                "kind": action.kind,
                "status": "failure",
                "error": "timeout",
                "outcome_unknown": True,
            }
        except BatchRejected as exc:
            code = (
                "credential_revoked"
                if exc.status_code == 401
                else "duplicate_step"
                if exc.status_code == 409
                else "denied"
            )
            return {"index": index, "kind": action.kind, "status": "failure", "error": code}
        except Exception:
            return {
                "index": index,
                "kind": action.kind,
                "status": "failure",
                "error": "step_failure",
                "outcome_unknown": True,
            }
        item = result["items"][0]
        if item.get("status") != "success":
            code = item.get("error") if isinstance(item.get("error"), str) else "step_failure"
            outcome: dict[str, Any] = {
                "index": index,
                "kind": action.kind,
                "status": "failure",
                "error": code,
            }
            if item.get("outcome_unknown") is True:
                outcome["outcome_unknown"] = True
            return outcome
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        outcome = {"index": index, "kind": action.kind, "status": "success"}
        if type(data.get("message_id")) is int:
            outcome["message_id"] = data["message_id"]
        return outcome

    @staticmethod
    def _batch_action(action: SendAction) -> Send:
        return Send(
            account_id=action.account_id,
            operation="send",
            chat_id=action.chat_id,
            text=action.text,
        )

    @staticmethod
    def _key(run_id: str, index: int) -> UUID:
        # Deterministic per run/step: a repeated step cannot duplicate an effect.
        return uuid.uuid5(uuid.NAMESPACE_URL, f"tg-control:workflow:{run_id}:{index}")

    def _finish(
        self,
        run_id: str,
        state: str,
        steps: list[dict[str, Any]],
        error: str | None,
        *,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        record_component_outcome("workflows", state, kind="run")
        encoded = self._bounded_steps(steps)
        with self.sessions() as session:
            run = session.get(WorkflowRun, run_id)
            if run is None:
                raise WorkflowError("run_not_found", 404)
            run.state = state
            run.steps = encoded
            run.error = error
            run.finished_at = now()
            if dry_run:
                run.trigger = "dry_run"
            audit.record(
                session,
                request_id=run.correlation_id or str(uuid.uuid4()),
                actor_kind="system",
                actor_id=None,
                event="workflow_run_finished",
                subject_client_id=None,
                decision=state,
                error_code=error,
            )
            session.commit()
            return serialize_run(run)

    @staticmethod
    def _bounded_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        import json

        clean = []
        for step in steps[:MAX_ACTIONS]:
            if not isinstance(step, dict):
                continue
            entry = {
                key: step[key]
                for key in ("index", "kind", "status", "error", "message_id", "outcome_unknown")
                if key in step
                and isinstance(step[key], (str, int, bool))
            }
            clean.append(entry)
        if len(json.dumps(clean)) > MAX_RESULT_BYTES:  # pragma: no cover - bounded above
            return clean[:MAX_ACTIONS]
        return clean

    # -- history and recovery ---------------------------------------------

    def runs(
        self,
        workflow_id: str,
        *,
        limit: int = 50,
        before: tuple[datetime, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Stable keyset listing: newest first, (started_at, id) tiebreaker."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise WorkflowError("invalid_request", 422)
        with self.sessions() as session:
            self._get(session, workflow_id)
            statement = (
                select(WorkflowRun)
                .where(WorkflowRun.workflow_id == workflow_id)
                .order_by(WorkflowRun.started_at.desc(), WorkflowRun.id.desc())
                .limit(limit)
            )
            if before is not None:
                started_at, identifier = before
                statement = statement.where(
                    or_(
                        WorkflowRun.started_at < started_at,
                        and_(
                            WorkflowRun.started_at == started_at,
                            WorkflowRun.id < identifier,
                        ),
                    )
                )
            rows = session.scalars(statement).all()
            return [serialize_run(row) for row in rows]

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            run = session.get(WorkflowRun, run_id) if isinstance(run_id, str) else None
            if run is None or len(run_id) != 36:
                raise WorkflowError("run_not_found", 404)
            return serialize_run(run)

    def prune_runs(
        self,
        *,
        older_than_days: int,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Delete finished workflow runs older than the retention window.

        A `running` or otherwise unfinished run is never pruned: its recovery
        semantics depend on the record still existing.
        """
        if type(older_than_days) is not int or not 1 <= older_than_days <= 36500:
            raise WorkflowError("invalid_request", 422)
        if type(dry_run) is not bool:
            raise WorkflowError("invalid_request", 422)
        cutoff = now() - timedelta(days=older_than_days)
        with self.sessions() as session:
            predicate = and_(
                WorkflowRun.state.in_(sorted(PRUNEABLE_RUN_STATES)),
                WorkflowRun.started_at < cutoff,
            )
            eligible = (
                session.scalar(select(func.count()).select_from(WorkflowRun).where(predicate)) or 0
            )
            deleted = 0
            if not dry_run and eligible:
                deleted = session.execute(delete(WorkflowRun).where(predicate)).rowcount or 0
                if deleted:
                    audit.record(
                        session,
                        request_id=str(uuid.uuid4()),
                        actor_kind="system",
                        actor_id=None,
                        event="workflow_runs_pruned",
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

    def recover_interrupted(self) -> list[dict[str, Any]]:
        """Resolve runs whose process died. Mutations may have occurred."""
        recovered: list[dict[str, Any]] = []
        with self.sessions() as session:
            rows = session.scalars(
                select(WorkflowRun).where(WorkflowRun.state == "running")
            ).all()
            for run in rows:
                run.state = "outcome_unknown"
                run.error = "interrupted"
                run.finished_at = now()
                audit.record(
                    session,
                    request_id=run.correlation_id or str(uuid.uuid4()),
                    actor_kind="system",
                    actor_id=None,
                    event="workflow_run_recovered",
                    decision="outcome_unknown",
                    error_code="interrupted",
                )
                recovered.append({"run_id": run.id, "state": run.state})
            session.commit()
        return recovered
