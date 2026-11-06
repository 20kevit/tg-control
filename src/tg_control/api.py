"""Authenticated synthetic gateway API and separate owner management plane."""

from __future__ import annotations

import asyncio
import logging
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
from .auth import Principal, authenticate, revoke_client, rotate_credential, verify_owner
from .config import Settings
from .core import BatchRejected, Gateway
from .db import Account, AuditEvent, Client, Grant, Setting, make_engine, make_sessions
from .mcp_server import RemoteBackend, create_mcp_server
from .policy import GrantInput
from .request_audit import BoundaryAudit, RequestAudit, request_id_context
from .schemas import Batch
from .security import SecurityBoundary


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
    # A zero TTL disables the in-memory message cache.
    cache_ttl_seconds: Annotated[int, Field(strict=True, ge=0, le=31_536_000)] = 3600
    media_retention_days: Annotated[int, Field(strict=True, ge=1, le=36500)] | None = 30


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
    fernet = Fernet(settings.encryption_key)
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
        task = None
        try:
            # Refuse an uninitialized or old schema rather than auto-migrating at startup.
            with engine.connect() as connection:
                revision = connection.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                if revision != "0003":
                    raise RuntimeError("database migrations are not up to date")
            with sessions() as session:
                stored = session.get(Setting, "retention")
                retention = RetentionSettings.model_validate(stored.value if stored else {})
            await gateway.configure_retention(
                retention.cache_ttl_seconds, retention.media_retention_days
            )
            await gateway.maintain_media()

            async def maintenance() -> None:
                ticks = 0
                while True:
                    await asyncio.sleep(60)
                    gateway.maintain_cache()
                    ticks += 1
                    if ticks % 60 == 0:
                        try:
                            await gateway.maintain_media()
                        except Exception:
                            # Avoid raw storage exceptions, which can contain sensitive information.
                            logging.getLogger("tg_control.maintenance").error(
                                "media maintenance failed"
                            )

            task = asyncio.create_task(maintenance())
            async with mcp.session_manager.run():
                yield
        finally:
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await gateway.close()
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
    app.state.sessions = sessions
    app.state.settings = settings

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

    @app.get("/v1/admin/audit", dependencies=[Depends(owner)])
    async def get_audit(
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        before_id: Annotated[int | None, Query(ge=1)] = None,
    ):
        with sessions() as session:
            query = select(AuditEvent).order_by(AuditEvent.id.desc()).limit(limit)
            if before_id is not None:
                query = query.where(AuditEvent.id < before_id)
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

    @app.get("/", include_in_schema=False)
    async def index():
        return RedirectResponse("/admin/")

    app.mount(
        "/admin", StaticFiles(directory=Path(__file__).parent / "static", html=True), name="admin"
    )
    # The MCP mount is last so explicit API and panel routes keep precedence.
    app.mount("/", mcp_app)
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
