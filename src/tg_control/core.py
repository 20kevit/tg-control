"""The only gateway execution path. Interfaces must never call the adapter directly."""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import asdict
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from . import audit
from .adapters.base import (
    AdapterError,
    Chat,
    ChatKind,
    RateLimited,
    SessionStatus,
    SessionUnavailable,
    TelegramAdapter,
    normalized_error_code,
    valid_retry_after,
)
from .auth import Principal, is_active
from .cache import ReadCache
from .capabilities import capability_catalog, visible_operations_for_scopes
from .db import Account, ExecutionKey, Grant, Setting
from .features import FEATURE_SPECS
from .policy import Permission, Scope, get_scope
from .schemas import MUTATIONS, Action, Batch, Forward, ListChats, Search

SESSION_MONITOR_KEY = "session_monitor_v1"
SESSION_MONITOR_STATES = {
    "ready",
    "disconnected",
    "reauth_required",
    "initializing",
    "intervention_required",
    "unavailable",
}
# This limits concurrent adapter checks, not the number of accounts being monitored.
SESSION_MONITOR_CONCURRENCY = 8


class BatchRejected(Exception):
    def __init__(self, status_code: int, items: list[dict[str, Any]], code: str):
        self.status_code = status_code
        self.items = items
        self.code = code
        super().__init__(code)


class RetryDeadline(AdapterError):
    def __init__(self, retry_after: float):
        super().__init__("rate_limit_timeout")
        self.retry_after = retry_after


class Gateway:
    def __init__(
        self,
        sessions: sessionmaker,
        adapter: TelegramAdapter,
        *,
        timeout_seconds: float = 30,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.sessions = sessions
        self.adapter = adapter
        self.timeout_seconds = timeout_seconds
        self.sleep = sleep
        self._cache = ReadCache()
        self._session_monitor_lock = asyncio.Lock()

    @staticmethod
    def _safe_adapter_error(exc: AdapterError, *, mutation: bool) -> tuple[str, bool]:
        try:
            raw_code = getattr(exc, "code", None)
            raw_unknown = getattr(exc, "outcome_unknown", None)
        except Exception:
            return "adapter_failure", mutation
        if type(raw_unknown) is not bool:
            return "adapter_failure", mutation
        code = normalized_error_code(raw_code)
        unknown = raw_unknown
        # A malformed or generic adapter failure does not establish that a write had no effect.
        if mutation and (
            type(raw_code) is not str or raw_code != code or code == "adapter_failure"
        ):
            unknown = True
        return code, unknown

    @staticmethod
    def _session_intervention(exc: SessionUnavailable) -> bool | None:
        try:
            intervention = exc.intervention_required
            expected = "reauth_required" if intervention else "disconnected"
            if (
                type(intervention) is bool
                and type(exc.code) is str
                and exc.code == expected
                and exc.outcome_unknown is False
            ):
                return intervention
        except Exception:
            pass
        return None

    async def _call(
        self,
        account_id: str,
        fn: Callable[[], Awaitable[Any]],
        *,
        mutation: bool = False,
        before_retry: Callable[[], Awaitable[None]] | None = None,
    ) -> Any:
        deadline = time.monotonic() + self.timeout_seconds
        immediate_retries = 0
        reconnected = False

        async def check_retry() -> None:
            if before_retry is None:
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AdapterError("timeout", outcome_unknown=False)
            try:
                await asyncio.wait_for(before_retry(), timeout=remaining)
            except TimeoutError:
                raise AdapterError("timeout", outcome_unknown=False) from None

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # No operation is currently in flight; prior retries were rejected.
                raise AdapterError("timeout", outcome_unknown=False)
            try:
                return await asyncio.wait_for(fn(), timeout=remaining)
            except RateLimited as exc:
                try:
                    delay = exc.retry_after
                    valid = (
                        type(exc.code) is str
                        and exc.code == "rate_limited"
                        and exc.outcome_unknown is False
                        and valid_retry_after(delay)
                    )
                except Exception:
                    valid = False
                if not valid:
                    raise AdapterError("adapter_failure", outcome_unknown=mutation) from None
                immediate_retries = immediate_retries + 1 if delay == 0 else 0
                remaining = deadline - time.monotonic()
                # Only repeated zero-duration rejections are capped to avoid a tight loop.
                if immediate_retries > 8 or delay >= remaining:
                    raise RetryDeadline(delay) from None
                try:
                    await asyncio.wait_for(self.sleep(delay), timeout=remaining)
                except TimeoutError:
                    raise RetryDeadline(delay) from None
                await check_retry()
            except SessionUnavailable as exc:
                intervention = self._session_intervention(exc)
                if intervention is None:
                    raise AdapterError("adapter_failure", outcome_unknown=mutation) from None
                if intervention or reconnected:
                    raise SessionUnavailable(intervention) from None
                reconnected = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AdapterError("timeout", outcome_unknown=False) from None
                try:
                    status = await asyncio.wait_for(
                        self.adapter.reconnect(account_id), timeout=remaining
                    )
                except TimeoutError:
                    raise AdapterError("reconnect_timeout") from None
                status = self._validated_session_status(status)
                if status.state != "ready":
                    raise SessionUnavailable(status.intervention_required) from None
                await check_retry()
            except TimeoutError:
                # A timeout while a mutation is in flight may have already caused an effect.
                raise AdapterError("timeout", outcome_unknown=mutation) from None
            except AdapterError as exc:
                code, unknown = self._safe_adapter_error(exc, mutation=mutation)
                raise AdapterError(code, outcome_unknown=unknown) from None

    @staticmethod
    def _permission(action: Action) -> Permission:
        feature = FEATURE_SPECS.get(action.operation)
        if feature is not None:
            return feature.permission
        if action.operation in {"set_title", "set_member_status"}:
            return Permission.ADMIN
        if action.operation in MUTATIONS:
            return Permission.WRITE
        return Permission.READ

    async def _authorize(
        self, session: Session, principal: Principal, action: Action
    ) -> tuple[bool, str | None]:
        if not is_active(session, principal):
            return False, "credential_revoked"
        account_id = str(action.account_id)
        scope = get_scope(session, principal.client_id, account_id, self._permission(action))
        if scope is None:
            return False, "denied"
        chat_id = getattr(action, "chat_id", None)
        chat = None
        if chat_id is not None:
            chat = await self._call(account_id, lambda: self.adapter.get_chat(account_id, chat_id))
            if chat is None or chat.id != chat_id or not scope.allows(chat):
                return False, "denied"
        source = None
        if isinstance(action, Forward):
            source_scope = get_scope(session, principal.client_id, account_id, Permission.READ)
            if source_scope is None:
                return False, "denied"
            source = await self._call(
                account_id, lambda: self.adapter.get_chat(account_id, action.source_chat_id)
            )
            if (
                source is None
                or source.id != action.source_chat_id
                or not source_scope.allows(source)
            ):
                return False, "denied"
        related_chats: list[tuple[Chat, Permission]] = []
        related_ids: list[tuple[str, Permission]] = []
        params_model = getattr(action, "params", None)
        if action.operation == "save_chat_folder" and params_model is not None:
            params = params_model.model_dump()
            related_ids.extend(
                (chat_id, Permission.WRITE)
                for chat_id in (*params.get("chat_ids", []), *params.get("excluded_chat_ids", []))
            )
        elif action.operation == "update_profile_details" and params_model is not None:
            channel_id = params_model.personal_channel_id
            if channel_id:
                related_ids.append((channel_id, Permission.READ))
        for related_id, permission in related_ids:
            related_chat = await self._call(
                account_id,
                lambda related_id=related_id: self.adapter.get_chat(account_id, related_id),
            )
            related_scope = get_scope(session, principal.client_id, account_id, permission)
            if (
                related_chat is None
                or related_chat.id != related_id
                or related_scope is None
                or not related_scope.allows(related_chat)
            ):
                return False, "denied"
            related_chats.append((related_chat, permission))
        # Adapter lookups above may have awaited while an owner revoked access.
        # Refresh credentials and both scopes after the final await, before approval.
        if not is_active(session, principal):
            return False, "credential_revoked"
        scope = get_scope(session, principal.client_id, account_id, self._permission(action))
        if scope is None or (chat is not None and not scope.allows(chat)):
            return False, "denied"
        if isinstance(action, Forward):
            source_scope = get_scope(session, principal.client_id, account_id, Permission.READ)
            if source is None or source_scope is None or not source_scope.allows(source):
                return False, "denied"
        for related_chat, permission in related_chats:
            related_scope = get_scope(session, principal.client_id, account_id, permission)
            if related_scope is None or not related_scope.allows(related_chat):
                return False, "denied"
        return True, None

    @staticmethod
    def _record_decision(
        session: Session,
        principal: Principal,
        request_id: str,
        action: Action,
        allowed: bool,
        error: str | None,
    ) -> None:
        audit.record(
            session,
            request_id=request_id,
            actor_kind="client",
            actor_id=principal.client_id,
            event=action.operation,
            account_id=str(action.account_id),
            chat_id=getattr(action, "chat_id", None),
            decision="allowed" if allowed else "denied",
            error_code=error,
        )

    async def list_accounts(
        self, principal: Principal, request_id: str | None = None
    ) -> list[dict]:
        request_id = request_id or str(uuid.uuid4())
        with self.sessions() as session:
            if not is_active(session, principal):
                raise BatchRejected(401, [], "credential_revoked")
            rows = (
                session.execute(
                    select(Account)
                    .join(Grant, Grant.account_id == Account.id)
                    .where(Grant.client_id == principal.client_id, Account.enabled.is_(True))
                    .order_by(Account.alias)
                )
                .scalars()
                .all()
            )
            # An empty assignment is not an enabled permission.
            visible = [
                a
                for a in rows
                if any(
                    get_scope(session, principal.client_id, a.id, permission)
                    for permission in Permission
                )
            ]
            audit.record(
                session,
                request_id=request_id,
                actor_kind="client",
                actor_id=principal.client_id,
                event="list_accounts",
                decision="allowed",
            )
            session.commit()
            return [{"id": a.id, "alias": a.alias} for a in visible]

    async def capabilities(
        self, principal: Principal, account_id: str, request_id: str | None = None
    ) -> dict[str, Any]:
        """Describe only operations available under this client's current scopes."""
        request_id = request_id or str(uuid.uuid4())
        with self.sessions() as session:
            if not is_active(session, principal):
                raise BatchRejected(401, [], "credential_revoked")
            scopes = {
                permission: get_scope(session, principal.client_id, account_id, permission)
                for permission in Permission
            }
            if not any(scopes.values()):
                raise BatchRejected(403, [], "denied")
            audit.record(
                session,
                request_id=request_id,
                actor_kind="client",
                actor_id=principal.client_id,
                event="capabilities.list",
                account_id=account_id,
                decision="allowed",
            )
            session.commit()

        # These optional adapter hooks expose only eligibility/chat metadata. Do
        # not route them through _call: capability discovery must never reconnect
        # a session. Missing, malformed, or unavailable chat metadata fails closed.
        async def metadata_lookup(method: Any) -> Any:
            if not callable(method):
                return None
            try:
                return await asyncio.wait_for(method(account_id), timeout=self.timeout_seconds)
            except Exception:
                return None

        profile_method = getattr(self.adapter, "capability_profile", None)
        chats_method = getattr(self.adapter, "capability_chats", None)
        profile_candidate, chats_candidate = await asyncio.gather(
            metadata_lookup(profile_method), metadata_lookup(chats_method)
        )
        profile = (
            {
                key: value
                for key, value in profile_candidate.items()
                if isinstance(key, str) and type(value) is bool
            }
            if isinstance(profile_candidate, dict)
            else {}
        )
        chats: list[Chat] = []
        if isinstance(chats_candidate, list):
            by_id: dict[str, Chat] = {}
            for chat in chats_candidate:
                if (
                    not isinstance(chat, Chat)
                    or type(chat.id) is not str
                    or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", chat.id) is None
                    or not isinstance(chat.kind, ChatKind)
                ):
                    by_id.clear()
                    break
                previous = by_id.get(chat.id)
                if previous is not None and previous.kind != chat.kind:
                    by_id.clear()
                    break
                # Titles and any other chat content are deliberately discarded.
                by_id[chat.id] = Chat(chat.id, chat.kind, "")
            chats = list(by_id.values())

        with self.sessions() as session:
            active = is_active(session, principal)
            current_scopes = {
                permission: get_scope(session, principal.client_id, account_id, permission)
                for permission in Permission
            }
            assigned = active and any(current_scopes.values())
            if not assigned:
                error = "denied" if active else "credential_revoked"
                audit.record(
                    session,
                    request_id=request_id,
                    actor_kind="client",
                    actor_id=principal.client_id,
                    event="capabilities.list",
                    account_id=account_id,
                    decision="denied",
                    error_code=error,
                )
                session.commit()
                raise BatchRejected(403 if active else 401, [], error)
            visible_operations = visible_operations_for_scopes(current_scopes, chats)
            audit.record(
                session,
                request_id=request_id,
                actor_kind="client",
                actor_id=principal.client_id,
                event="capabilities.list",
                account_id=account_id,
                decision="success",
            )
            session.commit()
        return capability_catalog(
            account_id,
            eligibility=profile,
            visible_operations=visible_operations,
        )

    async def admin_capabilities(
        self, account_id: str, request_id: str | None = None
    ) -> dict[str, Any]:
        """Owner-only capability view; reports synthetic eligibility only."""
        request_id = request_id or str(uuid.uuid4())
        with self.sessions() as session:
            account = session.get(Account, account_id)
            if account is None:
                raise BatchRejected(404, [], "not_found")
            audit.record(
                session,
                request_id=request_id,
                actor_kind="owner",
                actor_id=None,
                event="admin.capabilities.list",
                account_id=account_id,
                decision="allowed",
            )
            session.commit()
        profile: dict[str, bool] = {}
        capability_profile = getattr(self.adapter, "capability_profile", None)
        if callable(capability_profile):
            try:
                candidate = await asyncio.wait_for(
                    capability_profile(account_id), timeout=self.timeout_seconds
                )
            except Exception:
                candidate = None
            if isinstance(candidate, dict):
                profile = {
                    key: value
                    for key, value in candidate.items()
                    if isinstance(key, str) and type(value) is bool
                }
        return capability_catalog(account_id, eligibility=profile)

    async def preauthorize(
        self, principal: Principal, batch: Batch, request_id: str | None = None
    ) -> list[dict]:
        """Authorization-only evaluation for delayed work.

        Performs no adapter mutation and creates no execution key. Audit records are
        committed even when every action is allowed, so delayed job creation is
        traceable. Callers must still execute through :meth:`execute`, which
        rechecks authorization immediately before the effect.
        """
        request_id = request_id or str(uuid.uuid4())
        with self.sessions() as session:
            if not is_active(session, principal):
                raise BatchRejected(401, [], "credential_revoked")
            results = []
            for action in batch.actions:
                try:
                    allowed, error = await self._authorize(session, principal, action)
                except AdapterError as exc:
                    allowed, error = False, self._safe_adapter_error(exc, mutation=False)[0]
                except Exception:
                    allowed, error = False, "adapter_failure"
                self._record_decision(session, principal, request_id, action, allowed, error)
                results.append({"operation": action.operation, "allowed": allowed, "error": error})
            session.commit()
            return results

    async def execute(
        self, principal: Principal, batch: Batch, request_id: str | None = None
    ) -> dict:
        request_id = request_id or str(uuid.uuid4())
        with self.sessions() as session:
            if not is_active(session, principal):
                raise BatchRejected(401, [], "credential_revoked")
            decisions: list[tuple[bool, str | None]] = []
            # Resolve and authorize ALL targets, including forward source, before any effect.
            for action in batch.actions:
                try:
                    allowed, error = await self._authorize(session, principal, action)
                except AdapterError as exc:
                    allowed, error = False, self._safe_adapter_error(exc, mutation=False)[0]
                except Exception:
                    allowed, error = False, "adapter_failure"
                decisions.append((allowed, error))
                self._record_decision(session, principal, request_id, action, allowed, error)
            session.commit()  # Audit failure prevents all effects.
            if not all(allowed for allowed, _ in decisions):
                items = [
                    {
                        "index": i,
                        "operation": action.operation,
                        "status": "not_executed" if allowed else "denied",
                        "error": "batch_not_authorized" if allowed else error,
                    }
                    for i, (action, (allowed, error)) in enumerate(zip(batch.actions, decisions))
                ]
                # A disconnected session is not an authorization denial.
                errors = {error for _, error in decisions if error}
                status = 401 if "credential_revoked" in errors else 403
                if errors - {"denied", "credential_revoked"}:
                    status = 503
                raise BatchRejected(status, items, "batch_precondition_failed")
            if any(action.operation in MUTATIONS for action in batch.actions):
                session.add(
                    ExecutionKey(client_id=principal.client_id, key=str(batch.idempotency_key))
                )
                try:
                    session.commit()  # Reserve before the first effect, across processes/restarts.
                except IntegrityError:
                    session.rollback()
                    audit.record(
                        session,
                        request_id=request_id,
                        actor_kind="client",
                        actor_id=principal.client_id,
                        event="batch",
                        decision="denied",
                        error_code="duplicate_request",
                    )
                    session.commit()
                    raise BatchRejected(409, [], "duplicate_request") from None
            results = []
            for index, action in enumerate(batch.actions):
                try:

                    async def guard() -> None:
                        allowed, error = await self._authorize(session, principal, action)
                        self._record_decision(
                            session, principal, request_id, action, allowed, error
                        )
                        session.commit()  # Fail closed before effects and after retry waits.
                        if not allowed:
                            raise AdapterError(error or "denied")

                    await guard()
                    data = await self._execute_one(session, principal, action, guard, request_id)
                    if action.operation not in MUTATIONS:
                        # Do not return content if access was revoked during an in-flight read.
                        await guard()
                    item = {
                        "index": index,
                        "operation": action.operation,
                        "status": "success",
                        "data": data,
                    }
                    outcome, error = "success", None
                except AdapterError as exc:
                    mutation = action.operation in MUTATIONS
                    code, unknown = self._safe_adapter_error(exc, mutation=mutation)
                    retry_after = None
                    if isinstance(exc, RetryDeadline):
                        try:
                            delay = getattr(exc, "retry_after", None)
                        except Exception:
                            delay = None
                        if (
                            code == "rate_limit_timeout"
                            and not unknown
                            and valid_retry_after(delay)
                        ):
                            retry_after = delay
                        else:
                            code, unknown = "adapter_failure", mutation
                    item = {
                        "index": index,
                        "operation": action.operation,
                        "status": "failure",
                        "error": code,
                        "outcome_unknown": unknown,
                    }
                    if retry_after is not None:
                        item["retry_after"] = retry_after
                    outcome, error = "failure", code
                except Exception:
                    # Never surface upstream messages (which may contain content or secrets).
                    item = {
                        "index": index,
                        "operation": action.operation,
                        "status": "failure",
                        "error": "adapter_failure",
                        "outcome_unknown": action.operation in MUTATIONS,
                    }
                    outcome, error = "failure", "adapter_failure"
                audit.record(
                    session,
                    request_id=request_id,
                    actor_kind="client",
                    actor_id=principal.client_id,
                    event=action.operation,
                    account_id=str(action.account_id),
                    chat_id=getattr(action, "chat_id", None),
                    decision=outcome,
                    error_code=error,
                )
                session.commit()
                results.append(item)
            await self._delivery_recheck(session, principal, batch, results, request_id)
            return {"request_id": request_id, "items": results}

    async def _delivery_recheck(
        self,
        session: Session,
        principal: Principal,
        batch: Batch,
        results: list[dict],
        request_id: str,
    ) -> None:
        # Batch execution may await other actions after a read. Resolve all trusted chat
        # metadata first, then recheck current scopes synchronously before returning content.
        needed: set[tuple[str, str]] = set()
        for action, item in zip(batch.actions, results):
            if item["status"] != "success" or action.operation in MUTATIONS:
                continue
            account_id = str(action.account_id)
            chat_id = getattr(action, "chat_id", None)
            if chat_id is not None:
                needed.add((account_id, chat_id))
            elif isinstance(action, ListChats):
                needed.update((account_id, chat["id"]) for chat in item["data"]["chats"])
            elif isinstance(action, Search):
                needed.update((account_id, chat["chat_id"]) for chat in item["data"]["chats"])
            elif action.operation == "list_discussion_eligible_chats":
                needed.update((account_id, chat["chat_id"]) for chat in item["data"]["chats"])
            elif action.operation == "get_profile":
                channel_id = item["data"].get("profile", {}).get("personal_channel_id")
                if channel_id:
                    needed.add((account_id, channel_id))
            elif action.operation == "list_chat_folders":
                for folder in item["data"]["folders"]:
                    needed.update(
                        (account_id, related_id)
                        for related_id in (
                            *folder.get("chat_ids", []),
                            *folder.get("excluded_chat_ids", []),
                        )
                    )
            elif action.operation == "get_discussion" and item["data"].get("chat_id"):
                needed.add((account_id, item["data"]["chat_id"]))
        resolved: dict[tuple[str, str], Chat | None] = {}
        errors: dict[tuple[str, str], str] = {}
        for account_id, chat_id in needed:
            try:
                chat = await self._call(
                    account_id, lambda: self.adapter.get_chat(account_id, chat_id)
                )
                resolved[(account_id, chat_id)] = chat if chat and chat.id == chat_id else None
            except AdapterError as exc:
                errors[(account_id, chat_id)] = self._safe_adapter_error(exc, mutation=False)[0]
            except Exception:
                errors[(account_id, chat_id)] = "adapter_failure"
        for action, item in zip(batch.actions, results):
            if item["status"] != "success" or action.operation in MUTATIONS:
                continue
            account_id = str(action.account_id)
            active = is_active(session, principal)
            scope = (
                get_scope(session, principal.client_id, account_id, self._permission(action))
                if active
                else None
            )
            chat_id = getattr(action, "chat_id", None)
            error = None if scope else ("credential_revoked" if not active else "denied")
            if chat_id is not None and error is None:
                key = (account_id, chat_id)
                chat = resolved.get(key)
                error = errors.get(key) or (None if chat and scope.allows(chat) else "denied")
            elif error is None and (
                isinstance(action, (ListChats, Search))
                or action.operation == "list_discussion_eligible_chats"
            ):
                chat_key = "id" if isinstance(action, ListChats) else "chat_id"
                candidates = item["data"]["chats"]
                failure = next(
                    (
                        errors[(account_id, chat[chat_key])]
                        for chat in candidates
                        if (account_id, chat[chat_key]) in errors
                    ),
                    None,
                )
                if failure:
                    error = failure
                else:
                    visible = []
                    for candidate in candidates:
                        trusted = resolved.get((account_id, candidate[chat_key]))
                        allowed = bool(trusted and scope.allows(trusted))
                        audit.record(
                            session,
                            request_id=request_id,
                            actor_kind="client",
                            actor_id=principal.client_id,
                            event="delivery_scope",
                            account_id=account_id,
                            chat_id=candidate[chat_key],
                            decision="allowed" if allowed else "denied",
                            error_code=None if allowed else "denied",
                        )
                        if allowed:
                            visible.append(candidate)
                    item["data"]["chats"] = visible
            elif error is None and action.operation == "list_chat_folders":
                for folder in item["data"]["folders"]:
                    for key in ("chat_ids", "excluded_chat_ids"):
                        visible_ids = []
                        for related_id in folder.get(key, []):
                            trusted = resolved.get((account_id, related_id))
                            allowed = bool(trusted and scope.allows(trusted))
                            audit.record(
                                session,
                                request_id=request_id,
                                actor_kind="client",
                                actor_id=principal.client_id,
                                event="delivery_scope",
                                account_id=account_id,
                                chat_id=related_id,
                                decision="allowed" if allowed else "denied",
                                error_code=None if allowed else "denied",
                            )
                            if allowed:
                                visible_ids.append(related_id)
                        folder[key] = visible_ids
            elif error is None and action.operation == "get_profile":
                profile = item["data"].get("profile", {})
                channel_id = profile.get("personal_channel_id")
                if channel_id:
                    trusted = resolved.get((account_id, channel_id))
                    allowed = bool(trusted and scope.allows(trusted))
                    audit.record(
                        session,
                        request_id=request_id,
                        actor_kind="client",
                        actor_id=principal.client_id,
                        event="delivery_scope",
                        account_id=account_id,
                        chat_id=channel_id,
                        decision="allowed" if allowed else "denied",
                        error_code=None if allowed else "denied",
                    )
                    if not allowed:
                        profile["personal_channel_id"] = None
            if error is None and action.operation == "get_discussion":
                target_id = item["data"].get("chat_id")
                if target_id:
                    trusted = resolved.get((account_id, target_id))
                    allowed = bool(trusted and scope.allows(trusted))
                    audit.record(
                        session,
                        request_id=request_id,
                        actor_kind="client",
                        actor_id=principal.client_id,
                        event="delivery_scope",
                        account_id=account_id,
                        chat_id=target_id,
                        decision="allowed" if allowed else "denied",
                        error_code=None if allowed else "denied",
                    )
                    if not allowed:
                        item["data"]["chat_id"] = None
                        item["data"]["available"] = False
            if error:
                item.pop("data", None)
                item.update(status="failure", error=error, outcome_unknown=False)
            audit.record(
                session,
                request_id=request_id,
                actor_kind="client",
                actor_id=principal.client_id,
                event="delivery_recheck",
                account_id=account_id,
                chat_id=chat_id,
                decision="denied" if error else "allowed",
                error_code=error,
            )
        session.commit()

    async def _read_cached(
        self,
        account_id: str,
        operation: str,
        chat_id: str,
        params: dict,
        guard: Callable[[], Awaitable[None]],
    ) -> dict:
        key = self._cache.key(account_id, operation, chat_id, params)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        generation = self._cache.generation(account_id)
        result = await self._call(
            account_id,
            lambda: self.adapter.execute(account_id, operation, chat_id, params),
            before_retry=guard,
        )
        self._cache.put(key, result, generation)
        return result

    async def _execute_one(
        self,
        session: Session,
        principal: Principal,
        action: Action,
        guard: Callable[[], Awaitable[None]],
        request_id: str,
    ) -> dict:
        account_id = str(action.account_id)
        if isinstance(action, ListChats) or (isinstance(action, Search) and action.chat_id is None):
            chats: list[Chat] = await self._call(
                account_id, lambda: self.adapter.list_chats(account_id), before_retry=guard
            )
            visible: list[Chat] = []
            for chat in chats:
                session.expire_all()
                if not is_active(session, principal):
                    raise AdapterError("credential_revoked")
                scope: Scope | None = get_scope(
                    session, principal.client_id, account_id, Permission.READ
                )
                if scope and scope.allows(chat):
                    visible.append(chat)
            if isinstance(action, ListChats):
                return {"chats": [asdict(chat) for chat in visible]}
            results = []
            for chat in visible:

                async def chat_guard() -> None:
                    # Check the specific chat again after every retry delay, not just the account.
                    await guard()
                    current = await self._call(
                        account_id, lambda: self.adapter.get_chat(account_id, chat.id)
                    )
                    session.expire_all()
                    scope = get_scope(session, principal.client_id, account_id, Permission.READ)
                    allowed = bool(
                        is_active(session, principal)
                        and current
                        and current.id == chat.id
                        and scope
                        and scope.allows(current)
                    )
                    audit.record(
                        session,
                        request_id=request_id,
                        actor_kind="client",
                        actor_id=principal.client_id,
                        event="search_scope",
                        account_id=account_id,
                        chat_id=chat.id,
                        decision="allowed" if allowed else "denied",
                        error_code=None if allowed else "denied",
                    )
                    session.commit()
                    if not allowed:
                        raise AdapterError("denied")

                await chat_guard()
                params = action.model_dump(exclude={"operation", "account_id", "chat_id"})
                result = await self._read_cached(account_id, "search", chat.id, params, chat_guard)
                # Recheck this exact chat after the adapter/cache read, before retaining the result.
                await chat_guard()
                results.append({"chat_id": chat.id, **result})
            return {"chats": results, "history_complete": False, "sync_state": "synthetic"}
        params = action.model_dump(exclude={"operation", "account_id", "chat_id"})
        chat_id = getattr(action, "chat_id", None)
        if action.operation in {"history", "search", "get_message"}:
            return await self._read_cached(account_id, action.operation, chat_id, params, guard)
        mutation = action.operation in MUTATIONS
        if mutation:
            self._cache.invalidate(account_id)
        # Account-level operations have no caller-selected chat target. A private
        # sentinel is used only inside the adapter boundary; it is never resolved
        # as a Telegram chat or returned to the client.
        adapter_chat_id = chat_id or "__account__"
        try:
            return await self._call(
                account_id,
                lambda: self.adapter.execute(account_id, action.operation, adapter_chat_id, params),
                mutation=mutation,
                before_retry=guard,
            )
        finally:
            # Even an uncertain mutation must invalidate old results.
            if mutation:
                self._cache.invalidate(account_id)

    def storage_status(self) -> dict:
        return {"synthetic_persistent": self.adapter.persistent, "cache_mode": "memory"}

    async def close(self) -> None:
        await self.adapter.close()

    async def configure_retention(self, cache_ttl: int, media_days: int | None) -> None:
        self._cache.configure(cache_ttl)
        await self.adapter.configure_media_retention(media_days)

    def maintain_cache(self) -> None:
        self._cache.prune()

    async def maintain_media(self) -> None:
        # Include disabled accounts: disabling access must not exempt data from retention.
        with self.sessions() as session:
            account_ids = session.scalars(select(Account.id)).all()
        for account_id in account_ids:
            count = await self.adapter.prune_media(account_id)
            if count:
                self._cache.invalidate(account_id)
                with self.sessions() as session:
                    audit.record(
                        session,
                        request_id=str(uuid.uuid4()),
                        actor_kind="system",
                        actor_id=None,
                        event="media_retention",
                        account_id=account_id,
                        decision="success",
                    )
                    session.commit()

    @staticmethod
    def _validated_session_status(status: Any) -> SessionStatus:
        if (
            not isinstance(status, SessionStatus)
            or not isinstance(status.state, str)
            or status.state not in {"ready", "disconnected", "reauth_required", "initializing"}
            or type(status.intervention_required) is not bool
            or (status.state == "ready" and status.intervention_required)
        ):
            raise AdapterError("adapter_failure")
        if status.state == "reauth_required" and not status.intervention_required:
            return SessionStatus("reauth_required", intervention_required=True)
        return status

    async def _observe_session(self, account_id: str) -> tuple[str, str, str | None]:
        try:
            status = await asyncio.wait_for(
                self.adapter.status(account_id), timeout=self.timeout_seconds
            )
            status = self._validated_session_status(status)
        except SessionUnavailable as exc:
            intervention = self._session_intervention(exc)
            if intervention is None:
                return account_id, "unavailable", "adapter_failure"
            state = "reauth_required" if intervention else "disconnected"
            return account_id, state, state
        except TimeoutError:
            return account_id, "unavailable", "timeout"
        except AdapterError as exc:
            return account_id, "unavailable", self._safe_adapter_error(exc, mutation=False)[0]
        except Exception:
            # Never retain exception text, account content, or login details.
            return account_id, "unavailable", "adapter_failure"
        state = status.state
        if status.intervention_required and state != "reauth_required":
            state = "intervention_required"
        return account_id, state, None

    async def maintain_sessions(self) -> None:
        """Poll synthetic sessions without effects and persist metadata-only transitions.

        Checks are bounded per account and concurrency, with no account-count cap.
        The setting and transition events commit together. Multiple workers remain
        unsupported; the process lock prevents overlapping local maintenance runs.
        """
        async with self._session_monitor_lock:
            with self.sessions() as session:
                account_ids = session.scalars(
                    select(Account.id).where(Account.enabled.is_(True)).order_by(Account.id)
                ).all()
            observed: dict[str, tuple[str, str | None]] = {}
            for start in range(0, len(account_ids), SESSION_MONITOR_CONCURRENCY):
                checks = await asyncio.gather(
                    *(
                        self._observe_session(account_id)
                        for account_id in account_ids[start : start + SESSION_MONITOR_CONCURRENCY]
                    )
                )
                observed.update({account_id: (state, error) for account_id, state, error in checks})
            with self.sessions() as session:
                # Accounts may have been disabled while status checks were in flight.
                enabled = set(session.scalars(select(Account.id).where(Account.enabled.is_(True))))
                observed = {key: value for key, value in observed.items() if key in enabled}
                setting = session.get(Setting, SESSION_MONITOR_KEY)
                if setting is None:
                    previous: dict[str, str] = {}
                else:
                    value = setting.value
                    if (
                        not isinstance(value, dict)
                        or set(value) != {"states"}
                        or not isinstance(value["states"], dict)
                        or any(
                            not isinstance(key, str)
                            or not isinstance(state, str)
                            or state not in SESSION_MONITOR_STATES
                            for key, state in value["states"].items()
                        )
                    ):
                        raise ValueError("invalid session monitor state")
                    previous = value["states"]
                current = {key: state for key, (state, _error) in observed.items()}
                for account_id, (state, error) in observed.items():
                    old = previous.get(account_id)
                    if old != state and (old is not None or state != "ready"):
                        audit.record(
                            session,
                            request_id=str(uuid.uuid4()),
                            actor_kind="system",
                            actor_id=None,
                            event="session_transition",
                            account_id=account_id,
                            decision=state,
                            error_code=error,
                        )
                if setting is None and current:
                    session.add(Setting(key=SESSION_MONITOR_KEY, value={"states": current}))
                elif setting is not None and previous != current:
                    setting.value = {"states": current}
                session.commit()

    async def owner_account_status(self, account_id: str, *, reconnect: bool = False) -> dict:
        """Owner-only caller must authenticate before entering this core method."""
        with self.sessions() as session:
            account = session.get(Account, account_id)
            if not account or not account.enabled:
                raise LookupError("account not found")
            event = "account_reconnect" if reconnect else "account_status"
            request_id = str(uuid.uuid4())
            audit.record(
                session,
                request_id=request_id,
                actor_kind="owner",
                actor_id=None,
                event=event,
                account_id=account_id,
                decision="allowed",
            )
            session.commit()
            try:
                operation = (
                    self.adapter.reconnect(account_id)
                    if reconnect
                    else self.adapter.status(account_id)
                )
                status = self._validated_session_status(
                    await asyncio.wait_for(operation, timeout=self.timeout_seconds)
                )
            except SessionUnavailable as exc:
                intervention = self._session_intervention(exc)
                if intervention is not None:
                    failure = {
                        "state": "reauth_required" if intervention else "disconnected",
                        "intervention_required": intervention,
                        "outcome_unknown": False,
                    }
                else:
                    failure = {
                        "state": "adapter_failure",
                        "intervention_required": True,
                        "outcome_unknown": reconnect,
                    }
            except TimeoutError:
                failure = {
                    "state": "reconnect_timeout" if reconnect else "timeout",
                    "intervention_required": True,
                    "outcome_unknown": reconnect,
                }
            except AdapterError as exc:
                code, unknown = self._safe_adapter_error(exc, mutation=reconnect)
                failure = {
                    "state": code,
                    "intervention_required": True,
                    "outcome_unknown": unknown or reconnect,
                }
            except Exception:
                # Do not expose upstream text, which may contain sensitive data.
                failure = {
                    "state": "adapter_failure",
                    "intervention_required": True,
                    "outcome_unknown": reconnect,
                }
            else:
                failure = None
            if failure is not None:
                audit.record(
                    session,
                    request_id=request_id,
                    actor_kind="owner",
                    actor_id=None,
                    event=event,
                    account_id=account_id,
                    decision="failure",
                    error_code=failure["state"],
                )
                session.commit()
                return failure
            audit.record(
                session,
                request_id=request_id,
                actor_kind="owner",
                actor_id=None,
                event=event,
                account_id=account_id,
                decision="success",
            )
            session.commit()
            return asdict(status)
