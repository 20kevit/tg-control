"""Audit HTTP request starts and outcomes without logging URLs, bodies or credentials."""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from . import audit
from .auth import authenticate, verify_owner

request_id_context: ContextVar[str | None] = ContextVar("request_id", default=None)
logger = logging.getLogger("tg_control.audit")


class RequestAudit:
    def __init__(self, app: ASGIApp, *, sessions, owner_token: str):
        self.app = app
        self.sessions = sessions
        self.owner_token = owner_token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path.startswith("/v1/")
            or path.rstrip("/") == "/mcp"
            or path in {"/health/live", "/health/ready", "/openapi.json"}
        ):
            await self.app(scope, receive, send)
            return
        # The outermost observability middleware may already have assigned a
        # correlation id; reuse it so logs, audit and the client header agree.
        existing = scope.get("state", {}).get("request_id")
        request_id = str(existing) if isinstance(existing, str) and existing else str(uuid.uuid4())
        event = (
            "health_request"
            if path.startswith("/health/")
            else "schema_request"
            if path == "/openapi.json"
            else "http_request"
        )
        state = scope.setdefault("state", {})
        state["request_id"] = request_id
        state["request_audit_started"] = True
        context_token = request_id_context.set(request_id)
        actor_kind, actor_id = "anonymous", None
        try:
            auth_headers = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
            candidate = ""
            if len(auth_headers) == 1:
                try:
                    parts = auth_headers[0].decode("ascii").split(" ")
                    if len(parts) == 2 and parts[0].lower() == "bearer" and len(parts[1]) <= 256:
                        candidate = parts[1]
                except UnicodeDecodeError:
                    pass
            with self.sessions() as session:
                if candidate and verify_owner(candidate, self.owner_token):
                    actor_kind = "owner"
                elif candidate:
                    principal = authenticate(session, candidate)
                    if principal:
                        actor_kind, actor_id = "client", principal.client_id
                audit.record(
                    session,
                    request_id=request_id,
                    actor_kind=actor_kind,
                    actor_id=actor_id,
                    event=event,
                    decision="started",
                )
                if scope.get("path", "").rstrip("/") == "/mcp" and actor_kind != "client":
                    audit.record(
                        session,
                        request_id=request_id,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        event="mcp_authentication",
                        decision="denied",
                        error_code="invalid_credential",
                    )
                session.commit()
        except Exception:
            request_id_context.reset(context_token)
            body = json.dumps({"error": "audit_unavailable", "outcome_unknown": False}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 503,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        if scope.get("path", "").rstrip("/") == "/mcp" and actor_kind != "client":
            request_id_context.reset(context_token)
            body = b'{"error":"invalid_credential"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                        (b"www-authenticate", b"Bearer"),
                        (b"cache-control", b"no-store"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return
        status = 500

        async def capture(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            try:
                with self.sessions() as session:
                    audit.record(
                        session,
                        request_id=request_id,
                        actor_kind=actor_kind,
                        actor_id=actor_id,
                        event=event,
                        decision="success" if status < 400 else "failure",
                        error_code=None if status < 400 else f"http_{status}",
                    )
                    session.commit()
            except Exception:
                # Never print the exception: database drivers may include secrets or SQL parameters.
                logger.error("audit completion could not be persisted")
            request_id_context.reset(context_token)


class BoundaryAudit:
    """Record requests rejected before authentication/routing, without trusting headers."""

    def __init__(self, app: ASGIApp, *, sessions):
        self.app = app
        self.sessions = sessions

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def capture(message: Message) -> None:
            if (
                scope["type"] == "http"
                and message["type"] == "http.response.start"
                and message["status"] in {400, 403, 413}
                and not scope.get("state", {}).get("request_audit_started")
            ):
                try:
                    with self.sessions() as session:
                        audit.record(
                            session,
                            request_id=str(uuid.uuid4()),
                            actor_kind="anonymous",
                            actor_id=None,
                            event="boundary_rejection",
                            decision="denied",
                            error_code=f"http_{message['status']}",
                        )
                        session.commit()
                except Exception:
                    # A denial remains a denial even when storage is unavailable.
                    logger.error("boundary rejection could not be audited")
            await send(message)

        await self.app(scope, receive, capture)
