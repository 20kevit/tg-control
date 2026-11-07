"""Offline TDLib adapter boundary: authorization/update lifecycle harness.

This module is deliberately unable to reach Telegram:

- No native library, socket, HTTP client or credential is imported. All transport
  happens through an injected ``TdjsonTransport``; the shipped implementation of
  that protocol is a test fake.
- Only ``TdlibMode.OFFLINE`` can be constructed. ``LIVE`` raises
  ``ComplianceBlocked`` and there is no environment variable, configuration flag,
  developer override or feature toggle that changes that. A future live adapter
  requires a separate compliance decision and owner approval, not a switch.
- The authorization harness never accepts phone numbers, login codes, passwords,
  emails or payment data. Those methods do not exist on the type; the only way to
  reach ``ready`` is ``synthetic_authorize()``, which requires an explicit
  synthetic fixture account and carries no real credential.
- Updates are normalized into the adapter boundary type and bounded; raw TDJSON
  objects never leave this module.

The purpose is contract proof: authorization states, update ordering,
deduplication, pagination, protected-content refusal and normalized errors are
tested without any real account, data or network access.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from .base import (
    AdapterError,
    Chat,
    ChatKind,
    SessionStatus,
    SessionUnavailable,
    Update,
)
from .tdlib_read_model import classify_chat, passes_text_safety_screen

MAX_BUFFERED_UPDATES = 500
MAX_HISTORY_PAGE = 100


class ComplianceBlocked(RuntimeError):
    """Raised when anything tries to construct a live adapter."""


class TdlibMode(StrEnum):
    OFFLINE = "offline"
    LIVE = "live"


class AuthorizationState(StrEnum):
    WAIT_PARAMETERS = "wait_parameters"
    READY = "ready"
    CLOSING = "closing"
    CLOSED = "closed"
    LOGGING_OUT = "logging_out"
    REAUTH_REQUIRED = "reauth_required"


# Offline-only transitions. There is no path to a real login: parameter
# submission is synthetic and any credential-bearing state stays unsupported.
ALLOWED_TRANSITIONS: dict[AuthorizationState, frozenset[AuthorizationState]] = {
    AuthorizationState.WAIT_PARAMETERS: frozenset(
        {AuthorizationState.READY, AuthorizationState.REAUTH_REQUIRED, AuthorizationState.CLOSED}
    ),
    AuthorizationState.READY: frozenset(
        {
            AuthorizationState.CLOSING,
            AuthorizationState.LOGGING_OUT,
            AuthorizationState.REAUTH_REQUIRED,
            AuthorizationState.CLOSED,
        }
    ),
    AuthorizationState.CLOSING: frozenset({AuthorizationState.CLOSED}),
    AuthorizationState.LOGGING_OUT: frozenset({AuthorizationState.CLOSED}),
    AuthorizationState.REAUTH_REQUIRED: frozenset(
        {AuthorizationState.WAIT_PARAMETERS, AuthorizationState.CLOSED}
    ),
    AuthorizationState.CLOSED: frozenset(),
}

AUTHORIZATION_UPDATE_MAP: dict[str, AuthorizationState] = {
    "authorizationStateWaitTdlibParameters": AuthorizationState.WAIT_PARAMETERS,
    "authorizationStateReady": AuthorizationState.READY,
    "authorizationStateClosing": AuthorizationState.CLOSING,
    "authorizationStateClosed": AuthorizationState.CLOSED,
    "authorizationStateLoggingOut": AuthorizationState.LOGGING_OUT,
}
# States that would require a real credential are recognized only to be refused.
CREDENTIAL_STATES = frozenset(
    {
        "authorizationStateWaitPhoneNumber",
        "authorizationStateWaitCode",
        "authorizationStateWaitPassword",
        "authorizationStateWaitEmailAddress",
        "authorizationStateWaitEmailCode",
        "authorizationStateWaitOtherDeviceConfirmation",
        "authorizationStateWaitRegistration",
        "authorizationStateWaitPremiumPurchase",
    }
)


class TdjsonTransport(Protocol):
    """The only place bytes could ever be exchanged. Fakes implement this."""

    async def send(self, payload: dict[str, Any]) -> None: ...

    async def receive(self) -> dict[str, Any]: ...


@dataclass
class OfflineTdjsonTransport:
    """In-memory transport for tests. It cannot perform IO."""

    outbox: list[dict[str, Any]] = field(default_factory=list)
    inbox: deque[dict[str, Any]] = field(default_factory=deque)

    async def send(self, payload: dict[str, Any]) -> None:
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        self.outbox.append(payload)

    async def receive(self) -> dict[str, Any]:
        if not self.inbox:
            raise AdapterError("timeout")
        return self.inbox.popleft()

    def deliver(self, payload: dict[str, Any]) -> None:
        self.inbox.append(payload)


@dataclass
class _StoredMessage:
    message_id: int
    chat_id: str
    text: str
    sender_id: str | None = None


class OfflineTdlibAdapter:
    """TelegramAdapter-shaped boundary proof. Never usable against Telegram."""

    persistent = False

    def __init__(
        self,
        transport: TdjsonTransport,
        *,
        mode: str = TdlibMode.OFFLINE,
        synthetic_account_id: str | None = None,
    ):
        if str(mode) != str(TdlibMode.OFFLINE):
            # Fail closed: configuration cannot enable a live adapter.
            raise ComplianceBlocked("live TDLib adapter is not permitted")
        self._transport = transport
        self._account_id = synthetic_account_id
        self._state = AuthorizationState.WAIT_PARAMETERS
        self._chats: dict[str, dict[str, Any]] = {}
        self._messages: dict[str, list[_StoredMessage]] = {}
        self._updates: deque[Update] = deque(maxlen=MAX_BUFFERED_UPDATES)
        self._seen_upstream: deque[str] = deque(maxlen=MAX_BUFFERED_UPDATES * 2)
        self._sequence = 0
        self._closed = False

    # -- lifecycle ---------------------------------------------------------

    @property
    def authorization_state(self) -> AuthorizationState:
        return self._state

    async def status(self, account_id: str) -> SessionStatus:
        self._require_account(account_id)
        return SessionStatus(
            state=self._state.value,
            intervention_required=self._state == AuthorizationState.REAUTH_REQUIRED,
        )

    async def synthetic_authorize(self, fixture_marker: str) -> SessionStatus:
        """Move to ready using a synthetic fixture, never a real credential."""
        if type(fixture_marker) is not str or not fixture_marker.startswith("synthetic:"):
            raise AdapterError("denied")
        await self._transition(AuthorizationState.READY)
        return await self.status(self._account_id)

    async def reconnect(self, account_id: str) -> SessionStatus:
        self._require_account(account_id)
        if self._state not in {
            AuthorizationState.REAUTH_REQUIRED,
            AuthorizationState.CLOSING,
            AuthorizationState.CLOSED,
        }:
            raise SessionUnavailable(intervention_required=False)
        # Cannot self-authorize offline; a synthetic fixture is required again.
        await self._transition(AuthorizationState.WAIT_PARAMETERS)
        return await self.status(account_id)

    async def close(self) -> None:
        self._closed = True
        self._state = AuthorizationState.CLOSED

    async def configure_media_retention(self, days: int | None) -> None:
        if days is not None and (type(days) is not int or days < 1):
            raise ValueError("invalid retention")
        return None

    async def prune_media(self, account_id: str) -> int:
        self._require_ready(account_id)
        return 0

    # -- authorization updates --------------------------------------------

    async def apply_authorization_update(self, raw: dict[str, Any]) -> AuthorizationState:
        """Apply a TDJSON authorization update without accepting credentials."""
        if not isinstance(raw, dict) or raw.get("@type") != "updateAuthorizationState":
            raise AdapterError("unsupported_operation")
        state = raw.get("authorization_state")
        kind = state.get("@type") if isinstance(state, dict) else None
        if kind in CREDENTIAL_STATES:
            await self._transition(AuthorizationState.REAUTH_REQUIRED)
            # No parameter, code, password or other submission path exists.
            raise AdapterError("reauth_required")
        mapped = AUTHORIZATION_UPDATE_MAP.get(kind)
        if mapped is None:
            await self._transition(AuthorizationState.REAUTH_REQUIRED)
            raise AdapterError("reauth_required")
        await self._transition(mapped)
        return self._state

    async def _transition(self, target: AuthorizationState) -> None:
        if target not in ALLOWED_TRANSITIONS[self._state]:
            raise AdapterError("unsupported_operation")
        self._state = target

    # -- chats and history -------------------------------------------------

    async def register_chat(self, raw: dict[str, Any], supergroup: dict[str, Any] | None = None):
        """Register trusted metadata using the fail-closed read model classifier."""
        if self._state != AuthorizationState.READY:
            raise SessionUnavailable(intervention_required=False)
        kind: ChatKind | None = classify_chat(raw, supergroup)
        if kind is None:
            raise AdapterError("denied")
        if raw.get("has_protected_content") is not False:
            raise AdapterError("denied")
        if type(raw.get("message_auto_delete_time")) is not int:
            raise AdapterError("denied")
        if raw["message_auto_delete_time"] != 0:
            raise AdapterError("denied")
        chat = Chat(id=str(raw["id"]), kind=kind, title=str(raw.get("title", ""))[:128])
        self._chats[chat.id] = {"chat": chat, "raw": raw, "supergroup": supergroup}
        return chat

    async def list_chats(self, account_id: str) -> list[Chat]:
        self._require_ready(account_id)
        return [entry["chat"] for entry in self._chats.values()]

    async def get_chat(self, account_id: str, chat_id: str) -> Chat | None:
        self._require_ready(account_id)
        entry = self._chats.get(str(chat_id))
        return entry["chat"] if entry else None

    async def self_chat(self, account_id: str) -> Chat:
        self._require_ready(account_id)
        raise AdapterError("unsupported_operation")

    async def execute(
        self, account_id: str, operation: str, chat_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        self._require_ready(account_id)
        if operation not in {"history", "get_message"}:
            # Mutations are intentionally unreachable in the offline boundary.
            raise AdapterError("unsupported_operation")
        if not isinstance(params, dict):
            raise AdapterError("unsupported_operation")
        entry = self._chats.get(str(chat_id))
        if entry is None:
            raise AdapterError("not_found")
        if entry["supergroup"] is None:
            raise AdapterError("denied")
        messages = self._messages.get(str(chat_id), [])
        if operation == "get_message":
            message_id = params.get("message_id")
            if type(message_id) is not int or message_id <= 0:
                raise AdapterError("unsupported_operation")
            for message in messages:
                if message.message_id == message_id:
                    return {"operation": operation, "message": self._render(message)}
            raise AdapterError("not_found")
        limit = params.get("limit", 50)
        if type(limit) is not int or not 1 <= limit <= MAX_HISTORY_PAGE:
            raise AdapterError("unsupported_operation")
        before = params.get("before_id")
        if before is not None and (type(before) is not int or before <= 0):
            raise AdapterError("unsupported_operation")
        ordered = sorted(messages, key=lambda item: item.message_id, reverse=True)
        if before is not None:
            ordered = [message for message in ordered if message.message_id < before]
        return {
            "operation": operation,
            "messages": [self._render(message) for message in ordered[:limit]],
        }

    # -- update lifecycle --------------------------------------------------

    async def drain_updates(self, account_id: str, limit: int = 100) -> list[Update]:
        self._require_ready(account_id)
        if type(limit) is not int or not 1 <= limit <= MAX_BUFFERED_UPDATES:
            raise AdapterError("unsupported_operation")
        drained: list[Update] = []
        while self._updates and len(drained) < limit:
            drained.append(self._updates.popleft())
        return drained

    async def feed_update(self, raw: dict[str, Any]) -> Update | None:
        """Normalize one raw update. Unknown or unsafe updates are dropped (None)."""
        if self._state != AuthorizationState.READY:
            raise SessionUnavailable(intervention_required=False)
        if not isinstance(raw, dict) or not isinstance(raw.get("@type"), str):
            return None
        upstream_id = raw.get("@extra")
        if upstream_id is not None:
            if type(upstream_id) is not str:
                return None
            if upstream_id in self._seen_upstream:
                return None
            self._seen_upstream.append(upstream_id)
        if raw["@type"] == "updateAuthorizationState":
            await self.apply_authorization_update(raw)
            return None
        if raw["@type"] != "updateNewMessage":
            return None
        message = raw.get("message")
        if not isinstance(message, dict):
            return None
        chat_id = message.get("chat_id")
        message_id = message.get("id")
        if type(chat_id) is not int or type(message_id) is not int or message_id <= 0:
            return None
        entry = self._chats.get(str(chat_id))
        if entry is None:
            # Unregistered chats never produce normalized events.
            return None
        if not passes_text_safety_screen(entry["raw"], message, entry["supergroup"]):
            return None
        content = message.get("content")
        body = content.get("text") if isinstance(content, dict) else None
        text = str(body.get("text", ""))[:16384] if isinstance(body, dict) else ""
        self._messages.setdefault(str(chat_id), []).append(
            _StoredMessage(
                message_id=message_id,
                chat_id=str(chat_id),
                text=text,
                sender_id=str(message["sender_id"]) if "sender_id" in message else None,
            )
        )
        self._sequence += 1
        update = Update(
            event_type="message.created",
            account_id=str(self._account_id),
            chat_id=str(chat_id),
            payload={
                "message_id": message_id,
                "text": text,
                "sequence": self._sequence,
            },
            upstream_id=upstream_id,
        )
        self._updates.append(update)
        return update

    # -- helpers -----------------------------------------------------------

    def _render(self, message: _StoredMessage) -> dict[str, Any]:
        return {
            "message_id": message.message_id,
            "chat_id": message.chat_id,
            "text": message.text,
            "sender_id": message.sender_id,
        }

    def _require_account(self, account_id: str | None) -> None:
        if self._closed:
            raise SessionUnavailable(intervention_required=True)
        if account_id is None or str(account_id) != str(self._account_id):
            raise AdapterError("not_found")

    def _require_ready(self, account_id: str | None) -> None:
        self._require_account(account_id)
        if self._state != AuthorizationState.READY:
            raise SessionUnavailable(
                intervention_required=self._state == AuthorizationState.REAUTH_REQUIRED
            )


def build_adapter(mode: str = TdlibMode.OFFLINE) -> OfflineTdlibAdapter:
    """The single construction path. Live mode always raises ``ComplianceBlocked``."""
    return OfflineTdlibAdapter(OfflineTdjsonTransport(), mode=mode)
