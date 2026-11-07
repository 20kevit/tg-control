"""Authenticated synthetic gateway API and separate owner management plane."""

from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from uuid import UUID

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from . import audit
from .adapters.base import TelegramAdapter
from .adapters.synthetic import SyntheticAdapter
from .adapters.synthetic_store import SyntheticStore
from .auth import (
    Principal,
    active_principal,
    authenticate,
    revoke_client,
    rotate_credential,
    verify_owner,
)
from .commands import CommandError, CommandRouter, SaveRequest
from .config import Settings
from .core import BatchRejected, Gateway
from .db import (
    EXPECTED_SCHEMA_REVISION,
    Account,
    AuditEvent,
    Client,
    Grant,
    Setting,
    make_engine,
    make_sessions,
)
from .events import EventError, EventService, SubscriptionInput
from .instance_guard import InstanceGuard, InstanceGuardBoundary
from .job_kinds import register_first_party_kinds
from .jobs import JobError, Jobs, Schedule
from .mcp_server import RemoteBackend, create_mcp_server
from .media import MediaPipeline
from .monitoring import SessionMonitorProgress
from .policy import ChatId, GrantInput
from .request_audit import BoundaryAudit, RequestAudit, request_id_context
from .schemas import Batch
from .security import SecurityBoundary
from .workflows import RunRequest, WorkflowError, WorkflowInput, Workflows

SESSION_MONITOR_INTERVAL_SECONDS = 60
MEDIA_CLEANUP_INTERVAL_SECONDS = 3600
# Staged media is private and ephemeral unless an explicit directory is configured.
_default_staging = Path(tempfile.mkdtemp(prefix="tg-control-media-"))
os.chmod(_default_staging, 0o700)
EVENT_INGEST_INTERVAL_SECONDS = 10
SCHEDULER_INTERVAL_SECONDS = 15


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCreate(StrictModel):
    alias: Annotated[str, Field(min_length=1, max_length=80)]
    # Only the reserved NANP 555-0100...0199 range, with a synthetic 202 area code.
    phone_number: Annotated[str, Field(pattern=r"^\+120255501[0-9]{2}$")]


class AccountUpdate(StrictModel):
    alias: Annotated[str, Field(min_length=1, max_length=80)] | None = None
    enabled: bool | None = None

    @model_validator(mode="after")
    def require_change(self):
        if self.alias is None and self.enabled is None:
            raise ValueError("no changes")
        return self


class ClientCreate(StrictModel):
    name: Annotated[str, Field(min_length=1, max_length=80)]


class RetentionSettings(StrictModel):
    audit_retention_days: Annotated[int, Field(strict=True, ge=1, le=36500)] = 90
    event_retention_days: Annotated[int, Field(strict=True, ge=1, le=36500)] = 30
    # A zero TTL disables the in-memory message cache.
    cache_ttl_seconds: Annotated[int, Field(strict=True, ge=0, le=31_536_000)] = 3600
    media_retention_days: Annotated[int, Field(strict=True, ge=1, le=36500)] | None = 30


class EventPullRequest(StrictModel):
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


class EventAckRequest(StrictModel):
    sequence: Annotated[int, Field(strict=True, ge=0)]


class EventNackRequest(StrictModel):
    event_id: Annotated[str, Field(min_length=36, max_length=36)]
    reason: Annotated[str, Field(max_length=32)] | None = None


class EventPauseRequest(StrictModel):
    paused: bool


class OwnerSaveRequest(StrictModel):
    client_id: UUID
    account_id: UUID
    chat_id: ChatId
    message_id: Annotated[int, Field(strict=True, ge=1)]
    idempotency_key: UUID


class JobCreateRequest(StrictModel):
    kind: Annotated[str, Field(min_length=1, max_length=48)]
    input: dict = Field(default_factory=dict)
    schedule: Schedule | None = None
    idempotency_key: UUID | None = None
    deadline_seconds: Annotated[int, Field(strict=True, ge=30, le=86400)] = 3600
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=10)] = 3


def _request_id() -> str:
    return str(uuid.uuid4())


def _bearer(request: Request) -> str:
    value = request.headers.get("authorization", "")
    parts = value.split(" ")
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1] or len(parts[1]) > 256:
        return ""
    return parts[1]


def create_app(settings: Settings, adapter: TelegramAdapter | None = None) -> FastAPI:
    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)
    store = (
        SyntheticStore(settings.synthetic_state_dir, settings.encryption_key)
        if adapter is None and settings.synthetic_state_dir is not None
        else None
    )
    selected_adapter = adapter if adapter is not None else SyntheticAdapter(store)
    gateway = Gateway(
        sessions, selected_adapter, timeout_seconds=settings.operation_timeout_seconds
    )
    staging_dir = settings.media_staging_dir or _default_staging
    media = MediaPipeline(staging_dir)
    commands = CommandRouter(
        gateway,
        selected_adapter,
        media,
        sessions,
        timeout_seconds=settings.operation_timeout_seconds,
    )
    events = EventService(
        sessions, selected_adapter, timeout_seconds=settings.operation_timeout_seconds
    )
    jobs = Jobs(
        sessions,
        handler_timeout_seconds=settings.operation_timeout_seconds,
    )
    register_first_party_kinds(jobs, gateway)
    workflows = Workflows(
        sessions, gateway, step_timeout_seconds=settings.operation_timeout_seconds
    )
    instance_guard = InstanceGuard(engine)
    fernet = Fernet(settings.encryption_key)
    monitor_progress = SessionMonitorProgress()
    mcp = create_mcp_server(RemoteBackend(gateway, sessions))
    # Outer SecurityBoundary/TrustedHostMiddleware validate Origin/Host for every MCP request.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        max_request_body_size=settings.max_request_bytes,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        tasks: list[asyncio.Task] = []
        try:
            # Refuse an uninitialized or old schema rather than auto-migrating at startup.
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                if revision != EXPECTED_SCHEMA_REVISION:
                    raise RuntimeError("database migrations are not up to date")
            instance_guard.acquire()
            with sessions() as session:
                stored = session.get(Setting, "retention")
                retention = RetentionSettings.model_validate(stored.value if stored else {})
            await gateway.configure_retention(
                retention.cache_ttl_seconds, retention.media_retention_days
            )
            await gateway.maintain_media()
            media.cleanup()
            workflows.recover_interrupted()
            # Resolve any work whose previous owner died before the scheduler starts.
            jobs.recover_expired_leases()

            async def maintenance() -> None:
                ticks = 0
                while True:
                    await asyncio.sleep(60)
                    if not instance_guard.check():
                        continue
                    try:
                        gateway.maintain_cache()
                    except Exception:
                        logging.getLogger("tg_control.maintenance").error(
                            "cache maintenance failed"
                        )
                    ticks += 1
                    if ticks % 60 == 0:
                        try:
                            media.cleanup()
                        except Exception:
                            logging.getLogger("tg_control.maintenance").error(
                                "media staging cleanup failed"
                            )
                        try:
                            await gateway.maintain_media()
                        except Exception:
                            # Avoid raw storage exceptions, which can contain sensitive information.
                            logging.getLogger("tg_control.maintenance").error(
                                "media maintenance failed"
                            )

            async def monitor_sessions() -> None:
                while True:
                    monitor_progress.begin()
                    try:
                        if not instance_guard.check():
                            raise RuntimeError("instance guard lost")
                        await gateway.maintain_sessions()
                    except Exception:
                        monitor_progress.finish(success=False)
                        logging.getLogger("tg_control.maintenance").error(
                            "session monitoring failed"
                        )
                    else:
                        monitor_progress.finish(success=True)
                    await asyncio.sleep(SESSION_MONITOR_INTERVAL_SECONDS)

            async def ingest_updates() -> None:
                while True:
                    try:
                        if instance_guard.check():
                            with sessions() as session:
                                account_ids = session.scalars(
                                    select(Account.id).where(Account.enabled.is_(True))
                                ).all()
                            for account_id in account_ids:
                                await events.ingest_updates(account_id)
                    except Exception:
                        # Never log upstream text or payloads.
                        logging.getLogger("tg_control.maintenance").error(
                            "event ingestion failed"
                        )
                    await asyncio.sleep(EVENT_INGEST_INTERVAL_SECONDS)

            async def run_scheduler() -> None:
                while True:
                    try:
                        if instance_guard.check():
                            await jobs.tick("gateway")
                    except Exception:
                        logging.getLogger("tg_control.maintenance").error("scheduler pass failed")
                    await asyncio.sleep(SCHEDULER_INTERVAL_SECONDS)

            tasks = [
                asyncio.create_task(maintenance()),
                asyncio.create_task(monitor_sessions()),
                asyncio.create_task(ingest_updates()),
                asyncio.create_task(run_scheduler()),
            ]
            app.state.session_monitor_task = tasks[1]
            async with mcp.session_manager.run():
                yield
        finally:
            jobs.close()
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
            try:
                await gateway.close()
            finally:
                try:
                    instance_guard.close()
                finally:
                    engine.dispose()

    app = FastAPI(
        title="TG-Control synthetic gateway",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.gateway = gateway
    app.state.instance_guard = instance_guard
    app.state.sessions = sessions
    app.state.settings = settings
    app.state.events = events
    app.state.media = media
    app.state.commands = commands
    app.state.workflows = workflows
    app.state.jobs = jobs

    def record_auth_failure() -> None:
        with sessions() as session:
            audit.record(
                session,
                request_id=request_id_context.get() or _request_id(),
                actor_kind="anonymous",
                actor_id=None,
                event="authentication",
                decision="denied",
                error_code="invalid_credential",
            )
            session.commit()

    async def client_principal(request: Request) -> Principal:
        token = _bearer(request)
        with sessions() as session:
            principal = authenticate(session, token)
        if principal is None:
            record_auth_failure()
            raise HTTPException(
                status_code=401, detail="invalid_credential", headers={"WWW-Authenticate": "Bearer"}
            )
        return principal

    async def owner(request: Request) -> None:
        if not verify_owner(_bearer(request), settings.owner_token):
            record_auth_failure()
            raise HTTPException(
                status_code=401, detail="invalid_credential", headers={"WWW-Authenticate": "Bearer"}
            )

    async def authenticated_schema_reader(request: Request) -> None:
        token = _bearer(request)
        if verify_owner(token, settings.owner_token):
            return
        with sessions() as session:
            if authenticate(session, token) is not None:
                return
        record_auth_failure()
        raise HTTPException(
            status_code=401, detail="invalid_credential", headers={"WWW-Authenticate": "Bearer"}
        )

    def owner_audit(
        session, event: str, *, account_id: str | None = None, subject_client_id: str | None = None
    ):
        audit.record(
            session,
            request_id=request_id_context.get() or _request_id(),
            actor_kind="owner",
            actor_id=None,
            event=event,
            account_id=account_id,
            subject_client_id=subject_client_id,
            decision="success",
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _exc: RequestValidationError):
        return JSONResponse({"error": "invalid_request"}, status_code=422)

    @app.exception_handler(HTTPException)
    async def http_error(_request: Request, exc: HTTPException):
        return JSONResponse(
            {"error": str(exc.detail)}, status_code=exc.status_code, headers=exc.headers
        )

    @app.exception_handler(BatchRejected)
    async def batch_error(_request: Request, exc: BatchRejected):
        return JSONResponse({"error": exc.code, "items": exc.items}, status_code=exc.status_code)

    @app.exception_handler(WorkflowError)
    async def workflow_error(_request: Request, exc: WorkflowError):
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)

    @app.exception_handler(CommandError)
    async def command_error(_request: Request, exc: CommandError):
        return JSONResponse(exc.as_body(), status_code=exc.status_code)

    @app.exception_handler(EventError)
    async def event_error(_request: Request, exc: EventError):
        return JSONResponse(exc.as_body(), status_code=exc.status_code)

    @app.exception_handler(JobError)
    async def job_error(_request: Request, exc: JobError):
        return JSONResponse({"error": exc.code}, status_code=exc.status_code)

    @app.exception_handler(IntegrityError)
    async def conflict(_request: Request, _exc: IntegrityError):
        return JSONResponse({"error": "conflict"}, status_code=409)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(_request: Request, _exc: SQLAlchemyError):
        return JSONResponse(
            {"error": "storage_unavailable", "outcome_unknown": True}, status_code=503
        )

    @app.get(
        "/openapi.json",
        include_in_schema=False,
        dependencies=[Depends(authenticated_schema_reader)],
    )
    async def openapi_schema():
        return app.openapi()

    @app.get("/health/live", dependencies=[Depends(owner)])
    async def live():
        return {"status": "ok", "mode": "synthetic"}

    @app.get("/health/ready", dependencies=[Depends(owner)])
    async def ready():
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return {"status": "ready", "mode": "synthetic"}

    @app.get("/v1/accounts")
    async def list_accounts(request: Request, principal: Principal = Depends(client_principal)):
        return {"accounts": await gateway.list_accounts(principal, request.state.request_id)}

    @app.post("/v1/execute")
    async def execute(
        request: Request, batch: Batch, principal: Principal = Depends(client_principal)
    ):
        return await gateway.execute(principal, batch, request.state.request_id)

    @app.get("/v1/admin/accounts", dependencies=[Depends(owner)])
    async def admin_accounts():
        with sessions() as session:
            accounts = session.scalars(select(Account).order_by(Account.alias)).all()
            owner_audit(session, "list_accounts")
            session.commit()
            return {
                "accounts": [
                    {
                        "id": a.id,
                        "alias": a.alias,
                        "phone_number": fernet.decrypt(a.phone_encrypted.encode()).decode(),
                        "enabled": a.enabled,
                    }
                    for a in accounts
                ]
            }

    @app.post("/v1/admin/accounts", status_code=201, dependencies=[Depends(owner)])
    async def create_account(data: AccountCreate):
        with sessions() as session:
            account = Account(
                alias=data.alias,
                phone_encrypted=fernet.encrypt(data.phone_number.encode()).decode(),
            )
            session.add(account)
            session.flush()
            owner_audit(session, "create_account", account_id=account.id)
            session.commit()
            return {
                "id": account.id,
                "alias": account.alias,
                "phone_number": data.phone_number,
                "enabled": account.enabled,
            }

    @app.patch("/v1/admin/accounts/{account_id}", dependencies=[Depends(owner)])
    async def update_account(account_id: UUID, data: AccountUpdate):
        with sessions() as session:
            account = session.get(Account, str(account_id))
            if not account:
                raise HTTPException(404, "not_found")
            if data.alias is not None:
                account.alias = data.alias
            if data.enabled is not None:
                account.enabled = data.enabled
            owner_audit(session, "update_account", account_id=account.id)
            session.commit()
            return {"id": account.id, "alias": account.alias, "enabled": account.enabled}

    @app.get("/v1/admin/accounts/{account_id}/status", dependencies=[Depends(owner)])
    async def account_status(account_id: UUID):
        try:
            return await gateway.owner_account_status(str(account_id))
        except LookupError:
            raise HTTPException(404, "not_found") from None

    @app.post("/v1/admin/accounts/{account_id}/reconnect", dependencies=[Depends(owner)])
    async def reconnect(account_id: UUID):
        try:
            return await gateway.owner_account_status(str(account_id), reconnect=True)
        except LookupError:
            raise HTTPException(404, "not_found") from None

    @app.get("/v1/admin/clients", dependencies=[Depends(owner)])
    async def admin_clients():
        with sessions() as session:
            clients = session.scalars(select(Client).order_by(Client.name)).all()
            owner_audit(session, "list_clients")
            session.commit()
            return {
                "clients": [{"id": c.id, "name": c.name, "enabled": c.enabled} for c in clients]
            }

    @app.post("/v1/admin/clients", status_code=201, dependencies=[Depends(owner)])
    async def create_client(data: ClientCreate):
        with sessions() as session:
            client = Client(name=data.name)
            session.add(client)
            session.flush()
            token = rotate_credential(session, client.id)
            owner_audit(session, "create_client", subject_client_id=client.id)
            session.commit()
            return {"id": client.id, "name": client.name, "credential": token}

    @app.post("/v1/admin/clients/{client_id}/rotate", dependencies=[Depends(owner)])
    async def rotate(client_id: UUID):
        with sessions() as session:
            try:
                token = rotate_credential(session, str(client_id))
            except LookupError:
                raise HTTPException(404, "not_found") from None
            owner_audit(session, "rotate_credential", subject_client_id=str(client_id))
            session.commit()
            return {"credential": token}

    @app.post("/v1/admin/clients/{client_id}/revoke", dependencies=[Depends(owner)])
    async def revoke(client_id: UUID):
        with sessions() as session:
            try:
                revoke_client(session, str(client_id))
            except LookupError:
                raise HTTPException(404, "not_found") from None
            owner_audit(session, "revoke_client", subject_client_id=str(client_id))
            session.commit()
            return {"revoked": True}

    @app.get(
        "/v1/admin/clients/{client_id}/accounts/{account_id}/policy", dependencies=[Depends(owner)]
    )
    async def get_policy(client_id: UUID, account_id: UUID):
        with sessions() as session:
            grant = session.get(Grant, (str(client_id), str(account_id)))
            if not grant:
                raise HTTPException(404, "not_found")
            owner_audit(
                session, "get_policy", account_id=str(account_id), subject_client_id=str(client_id)
            )
            session.commit()
            return {
                "read": grant.read_scope,
                "write": grant.write_scope,
                "admin": grant.admin_scope,
            }

    @app.put(
        "/v1/admin/clients/{client_id}/accounts/{account_id}/policy", dependencies=[Depends(owner)]
    )
    async def put_policy(client_id: UUID, account_id: UUID, data: GrantInput):
        with sessions() as session:
            cid, aid = str(client_id), str(account_id)
            client, account = session.get(Client, cid), session.get(Account, aid)
            if not client or not client.enabled or not account:
                raise HTTPException(404, "not_found")
            grant = session.get(Grant, (cid, aid))
            if not grant:
                grant = Grant(client_id=cid, account_id=aid)
                session.add(grant)
            grant.read_scope = data.read.model_dump(mode="json")
            grant.write_scope = data.write.model_dump(mode="json")
            grant.admin_scope = data.admin.model_dump(mode="json")
            owner_audit(session, "update_policy", account_id=aid, subject_client_id=cid)
            session.commit()
            return {"updated": True}

    @app.get("/v1/admin/session-monitor", dependencies=[Depends(owner)])
    async def session_monitor_status():
        # This is process-local task progress, not proof that any account is healthy.
        task = getattr(app.state, "session_monitor_task", None)
        with sessions() as session:
            owner_audit(session, "view_session_monitor")
            session.commit()
        return monitor_progress.snapshot(task_running=task is not None and not task.done())

    @app.get("/v1/admin/audit", dependencies=[Depends(owner)])
    async def get_audit(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        before_id: Annotated[int | None, Query(ge=1)] = None,
        event_filter: Annotated[
            str | None, Query(alias="event", min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
        ] = None,
    ):
        with sessions() as session:
            query = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
            if before_id is not None:
                query = query.where(AuditEvent.id < before_id)
            if event_filter is not None:
                query = query.where(AuditEvent.event == event_filter)
            events = session.scalars(query).all()
            owner_audit(session, "view_audit")
            session.commit()
            return {
                "events": [
                    {
                        "id": e.id,
                        "timestamp": e.timestamp.isoformat(),
                        "request_id": e.request_id,
                        "actor_kind": e.actor_kind,
                        "actor_id": e.actor_id,
                        "event": e.event,
                        "subject_client_id": e.subject_client_id,
                        "account_id": e.account_id,
                        "chat_id": e.chat_id,
                        "decision": e.decision,
                        "error_code": e.error_code,
                    }
                    for e in events
                ]
            }

    @app.get("/v1/admin/storage-status", dependencies=[Depends(owner)])
    async def storage_status():
        with sessions() as session:
            owner_audit(session, "view_storage_status")
            session.commit()
        return gateway.storage_status()

    @app.get("/v1/admin/settings", dependencies=[Depends(owner)])
    async def get_settings():
        with sessions() as session:
            setting = session.get(Setting, "retention")
            owner_audit(session, "get_settings")
            session.commit()
            return RetentionSettings.model_validate(setting.value if setting else {}).model_dump()

    @app.put("/v1/admin/settings", dependencies=[Depends(owner)])
    async def put_settings(data: RetentionSettings):
        with sessions() as session:
            setting = session.get(Setting, "retention")
            if setting is None:
                setting = Setting(key="retention", value=data.model_dump())
                session.add(setting)
            else:
                setting.value = data.model_dump()
            owner_audit(session, "update_settings")
            session.commit()
        await gateway.configure_retention(data.cache_ttl_seconds, data.media_retention_days)
        await gateway.maintain_media()
        return data.model_dump()

    @app.post("/v1/admin/maintenance/prune-audit", dependencies=[Depends(owner)])
    async def prune_audit():
        with sessions() as session:
            setting = session.get(Setting, "retention")
            config = RetentionSettings.model_validate(setting.value if setting else {})
            count = audit.prune(session, config.audit_retention_days)
            owner_audit(session, "prune_audit")
            session.commit()
            return {"deleted": count, "retention_days": config.audit_retention_days}

    # -- events ----------------------------------------------------------

    @app.post("/v1/events/subscriptions", status_code=201)
    async def create_subscription(
        data: SubscriptionInput, principal: Principal = Depends(client_principal)
    ):
        return await events.create_subscription(
            principal, data, request_id=request_id_context.get()
        )

    @app.get("/v1/events/subscriptions")
    async def list_subscriptions(principal: Principal = Depends(client_principal)):
        return {"subscriptions": events.list_subscriptions(principal)}

    @app.delete("/v1/events/subscriptions/{subscription_id}", status_code=204)
    async def delete_subscription(
        subscription_id: UUID, principal: Principal = Depends(client_principal)
    ):
        events.delete_subscription(principal, str(subscription_id))

    @app.post("/v1/events/subscriptions/{subscription_id}/pause")
    async def pause_subscription(
        subscription_id: UUID, principal: Principal = Depends(client_principal)
    ):
        return events.set_paused(principal, str(subscription_id), True)

    @app.post("/v1/events/subscriptions/{subscription_id}/resume")
    async def resume_subscription(
        subscription_id: UUID, principal: Principal = Depends(client_principal)
    ):
        return events.set_paused(principal, str(subscription_id), False)

    @app.post("/v1/events/subscriptions/{subscription_id}/pull")
    async def pull_events(
        subscription_id: UUID,
        data: EventPullRequest,
        principal: Principal = Depends(client_principal),
    ):
        return await events.pull(principal, str(subscription_id), limit=data.limit)

    @app.post("/v1/events/subscriptions/{subscription_id}/ack")
    async def ack_events(
        subscription_id: UUID,
        data: EventAckRequest,
        principal: Principal = Depends(client_principal),
    ):
        return events.ack(principal, str(subscription_id), data.sequence)

    @app.post("/v1/events/subscriptions/{subscription_id}/nack")
    async def nack_events(
        subscription_id: UUID,
        data: EventNackRequest,
        principal: Principal = Depends(client_principal),
    ):
        return events.nack(
            principal, str(subscription_id), data.event_id, reason=data.reason
        )

    @app.get("/v1/admin/events/dead-letters", dependencies=[Depends(owner)])
    async def event_dead_letters(
        subscription_id: Annotated[str | None, Query(max_length=36)] = None
    ):
        return {"dead_letters": events.dead_letters(subscription_id)}

    @app.post(
        "/v1/admin/events/dead-letters/{dead_letter_id}/replay",
        dependencies=[Depends(owner)],
    )
    async def replay_dead_letter(dead_letter_id: UUID):
        result = events.replay_dead_letter(str(dead_letter_id))
        with sessions() as session:
            owner_audit(session, "event_dead_letter_replay")
            session.commit()
        return result

    @app.post("/v1/admin/maintenance/prune-events", dependencies=[Depends(owner)])
    async def prune_events():
        with sessions() as session:
            setting = session.get(Setting, "retention")
            config = RetentionSettings.model_validate(setting.value if setting else {})
            deleted = events.prune(config.event_retention_days)
            owner_audit(session, "prune_events")
            session.commit()
        return {"deleted": deleted, "retention_days": config.event_retention_days}

    # -- durable jobs ----------------------------------------------------

    @app.post("/v1/jobs", status_code=201)
    async def create_job(
        data: JobCreateRequest, principal: Principal = Depends(client_principal)
    ):
        return await jobs.enqueue(
            principal,
            data.kind,
            data.input,
            schedule=data.schedule,
            idempotency_key=data.idempotency_key,
            deadline_seconds=data.deadline_seconds,
            max_attempts=data.max_attempts,
            request_id=request_id_context.get(),
        )

    @app.get("/v1/jobs")
    async def list_jobs(principal: Principal = Depends(client_principal)):
        return {"jobs": jobs.list_jobs(principal)}

    @app.get("/v1/jobs/{job_id}")
    async def inspect_job(job_id: UUID, principal: Principal = Depends(client_principal)):
        return jobs.inspect(principal, str(job_id))

    @app.post("/v1/jobs/{job_id}/pause")
    async def pause_job(job_id: UUID, principal: Principal = Depends(client_principal)):
        return jobs.set_paused(principal, str(job_id), True)

    @app.post("/v1/jobs/{job_id}/resume")
    async def resume_job(job_id: UUID, principal: Principal = Depends(client_principal)):
        return jobs.set_paused(principal, str(job_id), False)

    @app.post("/v1/jobs/{job_id}/cancel")
    async def cancel_job(job_id: UUID, principal: Principal = Depends(client_principal)):
        return jobs.cancel(principal, str(job_id))

    @app.get("/v1/admin/jobs", dependencies=[Depends(owner)])
    async def admin_jobs():
        return {"jobs": jobs.list_jobs(None)}

    @app.get("/v1/admin/jobs/{job_id}", dependencies=[Depends(owner)])
    async def admin_job(job_id: UUID):
        return jobs.inspect(None, str(job_id))

    @app.post("/v1/admin/jobs/{job_id}/cancel", dependencies=[Depends(owner)])
    async def admin_cancel_job(job_id: UUID):
        result = jobs.cancel(None, str(job_id))
        with sessions() as session:
            owner_audit(session, "job_cancel")
            session.commit()
        return result

    @app.post("/v1/admin/jobs/tick", dependencies=[Depends(owner)])
    async def scheduler_tick():
        result = await jobs.tick("owner")
        with sessions() as session:
            owner_audit(session, "scheduler_tick")
            session.commit()
        return result

    @app.post("/v1/admin/jobs/recover", dependencies=[Depends(owner)])
    async def scheduler_recover():
        result = jobs.recover_expired_leases()
        with sessions() as session:
            owner_audit(session, "job_recover")
            session.commit()
        return {"recovered": result}

    # -- reply-aware commands --------------------------------------------

    @app.get("/v1/commands")
    async def command_catalog(principal: Principal = Depends(client_principal)):
        return {"commands": commands.catalog()}

    @app.post("/v1/commands/save")
    async def save_reply(
        data: SaveRequest, principal: Principal = Depends(client_principal)
    ):
        return await commands.invoke(
            principal,
            "save",
            data,
            actor_kind="client",
            request_id=request_id_context.get(),
        )

    @app.post("/v1/admin/commands/save", dependencies=[Depends(owner)])
    async def owner_save_reply(data: OwnerSaveRequest):
        # Owner invocation applies the explicitly named client's policy; it is not
        # a policy bypass and cannot exceed that client's Read/Write grants.
        with sessions() as session:
            principal = active_principal(session, str(data.client_id))
        if principal is None:
            raise HTTPException(status_code=404, detail="client_not_found")
        return await commands.invoke(
            principal,
            "save",
            SaveRequest(
                account_id=data.account_id,
                chat_id=data.chat_id,
                message_id=data.message_id,
                idempotency_key=data.idempotency_key,
            ),
            actor_kind="owner",
            request_id=request_id_context.get(),
        )

    # -- owner-configured workflows --------------------------------------

    @app.get("/v1/admin/workflows", dependencies=[Depends(owner)])
    async def list_workflows():
        return {"workflows": workflows.list()}

    @app.post("/v1/admin/workflows", status_code=201, dependencies=[Depends(owner)])
    async def create_workflow(data: WorkflowInput):
        return workflows.create(data, request_id=request_id_context.get())

    @app.get("/v1/admin/workflows/{workflow_id}", dependencies=[Depends(owner)])
    async def get_workflow(workflow_id: UUID):
        return workflows.get(str(workflow_id))

    @app.put("/v1/admin/workflows/{workflow_id}", dependencies=[Depends(owner)])
    async def update_workflow(workflow_id: UUID, data: WorkflowInput):
        return workflows.update(
            str(workflow_id), data, request_id=request_id_context.get()
        )

    @app.post("/v1/admin/workflows/{workflow_id}/enable", dependencies=[Depends(owner)])
    async def enable_workflow(workflow_id: UUID):
        return workflows.set_enabled(
            str(workflow_id), True, request_id=request_id_context.get()
        )

    @app.post("/v1/admin/workflows/{workflow_id}/disable", dependencies=[Depends(owner)])
    async def disable_workflow(workflow_id: UUID):
        return workflows.set_enabled(
            str(workflow_id), False, request_id=request_id_context.get()
        )

    @app.post("/v1/admin/workflows/{workflow_id}/run", dependencies=[Depends(owner)])
    async def run_workflow(workflow_id: UUID, data: RunRequest):
        return await workflows.run(
            str(workflow_id), data, request_id=request_id_context.get()
        )

    @app.get("/v1/admin/workflows/{workflow_id}/runs", dependencies=[Depends(owner)])
    async def list_workflow_runs(workflow_id: UUID):
        return {"runs": workflows.runs(str(workflow_id))}

    @app.get("/v1/admin/workflow-runs/{run_id}", dependencies=[Depends(owner)])
    async def get_workflow_run(run_id: UUID):
        return workflows.get_run(str(run_id))

    @app.post("/v1/admin/workflows/recover", dependencies=[Depends(owner)])
    async def recover_workflows():
        recovered = workflows.recover_interrupted()
        with sessions() as session:
            owner_audit(session, "workflow_recover")
            session.commit()
        return {"recovered": recovered}

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/admin/")

    app.mount(
        "/admin", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="admin"
    )
    # The MCP mount is last so explicit API and panel routes keep precedence.
    app.mount("/", mcp_app)
    app.add_middleware(InstanceGuardBoundary, guard=instance_guard)
    app.add_middleware(RequestAudit, sessions=sessions, owner_token=settings.owner_token)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "MCP-Protocol-Version"],
        allow_credentials=False,
    )
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(settings.allowed_hosts))
    app.add_middleware(
        SecurityBoundary,
        allowed_origins=settings.allowed_origins,
        max_bytes=settings.max_request_bytes,
    )
    # Outer wrapper audits boundary rejections that never reach RequestAudit.
    app.add_middleware(BoundaryAudit, sessions=sessions)
    return app


def app_factory() -> FastAPI:
    return create_app(Settings.from_env())
