"""Normalized adapter types. No Telegram SDK types cross this boundary."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
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


@dataclass(frozen=True, slots=True)
class Update:
    """A normalized adapter update. Raw SDK objects never cross this boundary.

    ``upstream_id`` is a stable per-account identity for deduplication when the
    upstream source provides one. Payloads carry only the minimum schema fields.
    """

    event_type: str
    account_id: str
    chat_id: str | None
    payload: dict[str, Any]
    upstream_id: str | None = None
    occurred_at: float | None = None
    aggregate_type: str | None = None
    aggregate_id: str | None = None


ADAPTER_ERROR_CODES = frozenset(
    {
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
        "capability_unavailable",
        "permission_required",
        "content_protected",
        "poll_closed",
        "unsupported_platform",
        "account_restricted",
        "limit_exceeded",
        "compliance_blocked",
        "approval_required",
    }
)


def normalized_error_code(value: object) -> str:
    return value if type(value) is str and value in ADAPTER_ERROR_CODES else "adapter_failure"


def valid_retry_after(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return value >= 0 and isfinite(value)
    except (OverflowError, ValueError):
        return False


class AdapterError(Exception):
    """A safe, normalized error; never include upstream text in client output."""

    def __init__(self, code: str, *, outcome_unknown: bool = False):
        self.code = normalized_error_code(code)
        super().__init__(self.code)
        self.outcome_unknown = outcome_unknown if type(outcome_unknown) is bool else True


class RateLimited(AdapterError):
    """The adapter guarantees the operation was rejected, not applied."""

    def __init__(self, retry_after: float):
        if not valid_retry_after(retry_after):
            raise ValueError("retry_after must be a finite, non-negative duration")
        super().__init__("rate_limited")
        self.retry_after = retry_after


class SessionUnavailable(AdapterError):
    """Only for operations definitely not applied; uncertain mutations must stay unknown."""

    def __init__(self, intervention_required: bool):
        if type(intervention_required) is not bool:
            raise ValueError("invalid session intervention flag")
        super().__init__("reauth_required" if intervention_required else "disconnected")
        self.intervention_required = intervention_required


class UpdateSource(Protocol):
    """Optional adapter capability: normalized, account-scoped update intake.

    An adapter that cannot expose updates must raise ``AdapterError`` rather than
    return fabricated data. Draining is non-destructive only for the caller's
    copy; implementations own their bounded buffer and ordering caveats.
    """

    async def drain_updates(self, account_id: str, limit: int = 100) -> list[Update]: ...


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

    async def self_chat(self, account_id: str) -> Chat:
        """The account's Saved Messages chat. Resolved server-side, never caller-supplied."""
        ...

    async def execute(
        self, account_id: str, operation: str, chat_id: str, params: dict[str, Any]
    ) -> dict[str, Any]: ...
