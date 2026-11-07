"""Shared reply-aware command router. All effects go through Gateway Core.

Commands are invoked explicitly by an authenticated principal. Nothing in this
module inspects or reacts to arbitrary Telegram message content: a command is
never triggered by an incoming message that merely looks like a command.

Design notes (see ADR-009):
- ``/save`` resolves the replied-to message inside the explicitly selected
  account through the normal authorized Core read path, stages any media through
  the shared Media Pipeline, then sends to that account's Saved Messages through
  the same Core write path. There is no direct adapter shortcut.
- The destination write requires Write permission for the server-resolved Saved
  Messages chat. Retrieval requires Read for the source chat. An owner-invoked
  command applies an explicitly named client's policy; it never bypasses Policy.
- Audit records contain command/account/chat/message identifiers and outcomes
  only: never message bodies, captions, filenames or checksums.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Annotated, Any, Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from . import audit
from .adapters.base import Chat, TelegramAdapter
from .auth import Principal
from .core import BatchRejected, Gateway
from .media import MediaError, MediaPipeline, StagedMedia
from .policy import ChatId
from .schemas import Batch, GetMessage, Send, SendMedia

MAX_MESSAGE_TEXT = 16384
SAVE_COMMAND = "save"


class CommandError(Exception):
    """Normalized command failure; only safe codes are exposed."""

    def __init__(
        self,
        code: str,
        status_code: int,
        *,
        outcome_unknown: bool = False,
        retry_after: float | None = None,
    ):
        self.code = code
        self.status_code = status_code
        self.outcome_unknown = outcome_unknown
        self.retry_after = retry_after
        super().__init__(code)

    def as_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": self.code, "outcome_unknown": self.outcome_unknown}
        if self.retry_after is not None:
            body["retry_after"] = self.retry_after
        return body


class SaveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: UUID
    chat_id: ChatId
    message_id: Annotated[int, Field(strict=True, ge=1)]
    idempotency_key: UUID


@dataclass(frozen=True)
class CommandSpec:
    name: str
    version: int
    description: str
    reply_required: bool
    required_permissions: tuple[str, ...]
    principal_kinds: tuple[str, ...]


COMMANDS = (
    CommandSpec(
        name=SAVE_COMMAND,
        version=1,
        description=(
            "Save the replied-to message (text, caption and media) to this account's "
            "Saved Messages."
        ),
        reply_required=True,
        required_permissions=("read:source_chat", "write:saved_messages"),
        principal_kinds=("client", "owner-delegated-client"),
    ),
)


@dataclass(frozen=True)
class _ReadContent:
    text: str
    has_media: bool


class CommandRouter:
    def __init__(
        self,
        gateway: Gateway,
        adapter: TelegramAdapter,
        media: MediaPipeline,
        sessions,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        self.gateway = gateway
        self.adapter = adapter
        self.media = media
        self.sessions = sessions
        self.timeout_seconds = timeout_seconds
        self._handlers: dict[str, Callable[[Principal, Any, str], Awaitable[dict]]] = {
            SAVE_COMMAND: self._save
        }

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                "name": spec.name,
                "version": spec.version,
                "description": spec.description,
                "reply_required": spec.reply_required,
                "required_permissions": list(spec.required_permissions),
            }
            for spec in COMMANDS
        ]

    async def invoke(
        self,
        principal: Principal,
        name: str,
        request: Any,
        *,
        actor_kind: str = "client",
        request_id: str | None = None,
    ) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            raise CommandError("unknown_command", 404)
        request_id = request_id or str(uuid.uuid4())
        try:
            result = await handler(principal, request, request_id)
        except CommandError as exc:
            self._audit(principal, name, request, request_id, actor_kind, "failure", exc.code)
            raise
        except Exception:
            self._audit(
                principal, name, request, request_id, actor_kind, "failure", "command_failure"
            )
            raise CommandError("command_failure", 500) from None
        self._audit(principal, name, request, request_id, actor_kind, "success", None)
        return result

    def _audit(
        self,
        principal: Principal,
        name: str,
        request: Any,
        request_id: str,
        actor_kind: str,
        decision: str,
        error: str | None,
    ) -> None:
        account_id = getattr(request, "account_id", None)
        chat_id = getattr(request, "chat_id", None)
        # IDs and outcomes only: never content, captions, filenames or checksums.
        with self.sessions() as session:
            audit.record(
                session,
                request_id=request_id,
                actor_kind="owner" if actor_kind == "owner-delegated-client" else "client",
                actor_id=principal.client_id if actor_kind != "owner" else None,
                event=f"command_{name}",
                account_id=str(account_id) if account_id is not None else None,
                chat_id=chat_id,
                subject_client_id=principal.client_id if actor_kind == "owner" else None,
                decision=decision,
                error_code=error,
            )
            session.commit()

    async def _save(
        self, principal: Principal, request: SaveRequest, request_id: str
    ) -> dict[str, Any]:
        account_id = str(request.account_id)
        source_chat_id = request.chat_id
        content = await self._read_message(
            principal, account_id, source_chat_id, request.message_id, request_id
        )
        destination = await self._saved_chat(account_id)
        staged: StagedMedia | None = None
        try:
            if content.has_media:
                payload = await self._read_media(
                    principal, account_id, source_chat_id, request.message_id, request_id
                )
                staged = await self._stage(account_id, payload, principal, request, request_id)
                media_body = {
                    "filename": staged.filename,
                    "content_type": staged.content_type,
                    "data_base64": staged.as_base64(),
                }
                action: Any = SendMedia(
                    account_id=request.account_id,
                    operation="send_media",
                    chat_id=destination.id,
                    text=content.text[:MAX_MESSAGE_TEXT],
                    media=media_body,
                )
            elif content.text:
                action = Send(
                    account_id=request.account_id,
                    operation="send",
                    chat_id=destination.id,
                    text=content.text[:MAX_MESSAGE_TEXT],
                )
            else:
                raise CommandError("unsupported_content", 422)
            sent = await self._send(
                principal,
                Batch(actions=[action], idempotency_key=request.idempotency_key),
                request_id,
            )
        finally:
            if staged is not None:
                self.media.release(staged)
        message_id = sent.get("message_id")
        return {
            "command": SAVE_COMMAND,
            "version": 1,
            "account_id": account_id,
            "source_chat_id": source_chat_id,
            "source_message_id": request.message_id,
            "destination_chat_id": destination.id,
            "has_media": bool(staged is not None),
            "status": "success",
            **({"message_id": message_id} if type(message_id) is int else {}),
        }

    async def _read_message(
        self,
        principal: Principal,
        account_id: str,
        chat_id: str,
        message_id: int,
        request_id: str,
    ) -> _ReadContent:
        action = GetMessage(
            account_id=UUID(account_id),
            operation="get_message",
            chat_id=chat_id,
            message_id=message_id,
        )
        try:
            result = await self.gateway.execute(principal, Batch(actions=[action]), request_id)
        except BatchRejected as exc:
            raise self._batch_error(exc) from None
        item = result["items"][0]
        if item.get("status") != "success":
            raise self._item_error(item, default="unresolved_reply")
        message = item["data"].get("message")
        if not isinstance(message, dict) or message.get("id") != message_id:
            raise CommandError("unresolved_reply", 404)
        text = message.get("text")
        if not isinstance(text, str):
            text = ""
        return _ReadContent(text=text, has_media=message.get("media") is not None)

    async def _read_media(
        self,
        principal: Principal,
        account_id: str,
        chat_id: str,
        message_id: int,
        request_id: str,
    ) -> dict[str, Any]:
        action = GetMessage(
            account_id=UUID(account_id),
            operation="get_media",
            chat_id=chat_id,
            message_id=message_id,
        )
        try:
            result = await self.gateway.execute(principal, Batch(actions=[action]), request_id)
        except BatchRejected as exc:
            raise self._batch_error(exc) from None
        item = result["items"][0]
        if item.get("status") != "success":
            raise self._item_error(item, default="unavailable_source")
        media = item["data"].get("media")
        if not isinstance(media, dict):
            raise CommandError("unavailable_source", 404)
        return media

    async def _stage(
        self,
        account_id: str,
        payload: dict[str, Any],
        principal: Principal,
        request: SaveRequest,
        request_id: str,
    ) -> StagedMedia:
        async def guard() -> None:
            # Reauthorize source Read while bytes are being staged.
            batch = Batch(
                actions=[
                    GetMessage(
                        account_id=request.account_id,
                        operation="get_message",
                        chat_id=request.chat_id,
                        message_id=request.message_id,
                    )
                ]
            )
            results = await self.gateway.preauthorize(principal, batch, request_id)
            if not results[0]["allowed"]:
                raise CommandError("denied", 403)

        try:
            return await self.media.stage(account_id, payload, guard=guard)
        except MediaError as exc:
            raise CommandError(exc.code, exc.status_code) from None

    async def _send(self, principal: Principal, batch: Batch, request_id: str) -> dict[str, Any]:
        try:
            result = await self.gateway.execute(principal, batch, request_id)
        except BatchRejected as exc:
            if (
                exc.status_code == 403
                and exc.items
                and isinstance(exc.items[0], dict)
                and exc.items[0].get("error") == "denied"
            ):
                raise CommandError("destination_denied", 403) from None
            raise self._batch_error(exc) from None
        item = result["items"][0]
        if item.get("status") != "success":
            raise self._item_error(item, default="upload_failed")
        data = item.get("data")
        return data if isinstance(data, dict) else {}

    async def _saved_chat(self, account_id: str) -> Chat:
        try:
            chat = await asyncio.wait_for(
                self.adapter.self_chat(account_id), timeout=self.timeout_seconds
            )
        except Exception:
            raise CommandError("destination_unavailable", 503) from None
        if not isinstance(chat, Chat) or not chat.id:
            raise CommandError("destination_unavailable", 503)
        return chat

    @staticmethod
    def _batch_error(exc: BatchRejected) -> CommandError:
        if exc.code == "credential_revoked":
            return CommandError("credential_revoked", 401)
        if exc.code == "duplicate_request":
            return CommandError("duplicate_invocation", 409)
        if exc.status_code == 401:
            return CommandError("credential_revoked", 401)
        if exc.status_code == 409:
            return CommandError("duplicate_invocation", 409)
        if exc.status_code == 403:
            return CommandError("denied", 403)
        return CommandError("unavailable", 503)

    @staticmethod
    def _item_error(item: dict[str, Any], *, default: str) -> CommandError:
        code = item.get("error") if isinstance(item.get("error"), str) else default
        unknown = item.get("outcome_unknown") is True
        if code == "denied":
            return CommandError("destination_denied", 403)
        if code == "credential_revoked":
            return CommandError("credential_revoked", 401)
        if code == "not_found":
            return CommandError("unresolved_reply", 404)
        if code == "media_expired":
            return CommandError("unavailable_source", 404)
        if code == "rate_limit_timeout":
            retry_after = item.get("retry_after")
            return CommandError(
                "rate_limit",
                429,
                retry_after=float(retry_after)
                if type(retry_after) in (int, float)
                else None,
            )
        if code == "timeout":
            return CommandError("timeout", 504, outcome_unknown=unknown)
        if unknown:
            return CommandError("outcome_unknown", 503, outcome_unknown=True)
        return CommandError(default, 502)
