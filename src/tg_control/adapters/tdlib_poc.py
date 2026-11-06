"""Offline TDJSON correlation/state proof of concept; deliberately no native client or login.

This module cannot connect to Telegram. A real adapter is not enabled by configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import UUID, uuid4


@dataclass(frozen=True)
class AccountPaths:
    database: Path
    files: Path


def isolated_paths(root: Path, account_id: str) -> AccountPaths:
    # Only canonical UUIDs are accepted; caller-supplied path components are impossible.
    canonical = str(UUID(account_id))
    if canonical != account_id:
        raise ValueError("account ID must be a canonical UUID")
    directory = root / canonical
    return AccountPaths(directory / "database", directory / "files")


class OfflineTdJsonRouter:
    """Correlate synthetic TDJSON requests/responses per client without any IO."""

    def __init__(self) -> None:
        self.clients: dict[int, str] = {}
        self.pending: dict[tuple[int, str], str] = {}
        self.states: dict[str, str] = {}

    def register(self, client_id: int, account_id: str) -> None:
        UUID(account_id)
        if client_id in self.clients or account_id in self.clients.values():
            raise ValueError("client or account already registered")
        self.clients[client_id] = account_id
        self.states[account_id] = "initializing"

    def request(self, client_id: int, method: str) -> dict[str, str]:
        if client_id not in self.clients:
            raise ValueError("unknown client")
        request_id = uuid4().hex
        self.pending[(client_id, request_id)] = method
        return {"@type": method, "@extra": request_id}

    def receive(self, client_id: int, result: dict) -> tuple[str, str] | None:
        account_id = self.clients.get(client_id)
        if account_id is None:
            raise ValueError("unknown client")
        if result.get("@type") == "updateAuthorizationState":
            state = result.get("authorization_state", {}).get("@type")
            self.states[account_id] = {
                "authorizationStateReady": "ready",
                "authorizationStateClosed": "disconnected",
                "authorizationStateWaitCode": "reauth_required",
                "authorizationStateWaitPassword": "reauth_required",
                "authorizationStateWaitPhoneNumber": "reauth_required",
            }.get(state, "initializing")
            return None
        extra = result.get("@extra")
        if not isinstance(extra, str):
            return None
        method = self.pending.pop((client_id, extra), None)
        return (account_id, method) if method is not None else None
