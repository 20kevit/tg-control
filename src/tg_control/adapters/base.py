"""Normalized adapter types. No Telegram SDK types cross this boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol


class ChatKind(StrEnum):
    PUBLIC_CHANNEL = "public_channel"
    PUBLIC_GROUP = "public_group"
    PRIVATE_GROUP = "private_group"
    PRIVATE_DIRECT = "private_direct"


@dataclass(frozen=True, slots=True)
class Chat:
    id: str
    kind: ChatKind
    title: str


@dataclass(frozen=True, slots=True)
class SessionStatus:
    state: str
    intervention_required: bool = False


class AdapterError(Exception):
    """A safe, normalized error; never include upstream text in client output."""

    def __init__(self, code: str, *, outcome_unknown: bool = False):
        allowed = {
            "not_found",
            "unsupported_operation",
            "disconnected",
            "reauth_required",
            "timeout",
            "reconnect_timeout",
            "rate_limit_timeout",
            "rate_limited",
            "denied",
            "credential_revoked",
            "adapter_failure",
            "media_expired",
        }
        self.code = code if code in allowed else "adapter_failure"
        super().__init__(self.code)
        self.outcome_unknown = outcome_unknown


class RateLimited(AdapterError):
    """The adapter guarantees the operation was rejected, not applied."""

    def __init__(self, retry_after: float):
        if not 0 <= retry_after < float("inf"):
            raise ValueError("retry_after must be a finite, non-negative duration")
        super().__init__("rate_limited")
        self.retry_after = retry_after


class SessionUnavailable(AdapterError):
    def __init__(self, intervention_required: bool):
        super().__init__("reauth_required" if intervention_required else "disconnected")
        self.intervention_required = intervention_required


class TelegramAdapter(Protocol):
    """All lookups are explicitly account-scoped. This is not a public API."""

    @property
    def persistent(self) -> bool: ...

    async def close(self) -> None: ...

    async def configure_media_retention(self, days: int | None) -> None: ...

    async def prune_media(self, account_id: str) -> int: ...

    async def status(self, account_id: str) -> SessionStatus: ...

    async def reconnect(self, account_id: str) -> SessionStatus: ...

    async def list_chats(self, account_id: str) -> list[Chat]: ...

    async def get_chat(self, account_id: str, chat_id: str) -> Chat | None: ...

    async def execute(
        self, account_id: str, operation: str, chat_id: str, params: dict[str, Any]
    ) -> dict[str, Any]: ...
