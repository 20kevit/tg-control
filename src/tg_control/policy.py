"""Independent, deny-by-default scopes; caller-provided chat types are never used."""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .adapters.base import Chat, ChatKind
from .db import Account, Client, Grant

ChatId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]


class Permission(StrEnum):
    READ = "read"
    WRITE = "write"
    ADMIN = "admin"


class Scope(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    # Broad type grants deliberately cannot implicitly include new private chats.
    allow_types: list[ChatKind] = Field(default_factory=list)
    allow_chats: list[ChatId] = Field(default_factory=list)
    exclude_types: list[ChatKind] = Field(default_factory=list)
    exclude_chats: list[ChatId] = Field(default_factory=list)

    def allows(self, chat: Chat) -> bool:
        if not self.enabled or chat.id in self.exclude_chats or chat.kind in self.exclude_types:
            return False
        if chat.id in self.allow_chats:
            return True
        # Private types are allowed only when the individual chat was explicitly granted.
        if chat.kind in {ChatKind.PRIVATE_GROUP, ChatKind.PRIVATE_DIRECT}:
            return False
        return chat.kind in self.allow_types

    def model_post_init(self, __context) -> None:
        if any(t in {ChatKind.PRIVATE_GROUP, ChatKind.PRIVATE_DIRECT} for t in self.allow_types):
            raise ValueError("private chats require individual allow_chats entries")


class GrantInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    read: Scope = Field(default_factory=Scope)
    write: Scope = Field(default_factory=Scope)
    admin: Scope = Field(default_factory=Scope)


def get_scope(
    session: Session, client_id: str, account_id: str, permission: Permission
) -> Scope | None:
    client = session.get(Client, client_id)
    account = session.get(Account, account_id)
    grant = session.get(Grant, (client_id, account_id))
    if not client or not client.enabled or not account or not account.enabled or not grant:
        return None
    # Invalid/corrupt persisted policy must fail closed, not grant any access.
    try:
        scope = Scope.model_validate(getattr(grant, f"{permission.value}_scope"))
    except Exception:
        return None
    return scope if scope.enabled else None
