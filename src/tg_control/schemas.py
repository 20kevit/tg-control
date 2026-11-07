"""Strict action contracts; no arbitrary adapter methods, paths or claims."""

from __future__ import annotations

import base64
import binascii
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .policy import ChatId

MessageId = Annotated[int, Field(strict=True, ge=1)]
PageLimit = Annotated[int, Field(strict=True, ge=1, le=100)]  # TDLib page limit
Text = Annotated[str, Field(max_length=16384)]


class ActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    account_id: UUID


class ListChats(ActionBase):
    operation: Literal["list_chats"]


class Search(ActionBase):
    operation: Literal["search"]
    query: Annotated[str, Field(min_length=1, max_length=512)]
    chat_id: ChatId | None = None
    before_id: MessageId | None = None
    limit: PageLimit = 50


class History(ActionBase):
    operation: Literal["history"]
    chat_id: ChatId
    before_id: MessageId | None = None
    limit: PageLimit = 50


class GetMessage(ActionBase):
    operation: Literal["get_message", "get_media"]
    chat_id: ChatId
    message_id: MessageId


class Send(ActionBase):
    operation: Literal["send"]
    chat_id: ChatId
    text: Annotated[str, Field(min_length=1, max_length=16384)]


class Media(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    content_type: Annotated[str, Field(min_length=1, max_length=127)]
    data_base64: Annotated[str, Field(max_length=1_400_000)]

    @field_validator("data_base64")
    @classmethod
    def validate_data(cls, value: str) -> str:
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid base64") from exc
        if len(decoded) > 1_048_576:
            raise ValueError("synthetic media exceeds 1 MiB")
        return value


class SendMedia(ActionBase):
    operation: Literal["send_media"]
    chat_id: ChatId
    text: Text = ""
    media: Media


class Edit(ActionBase):
    operation: Literal["edit"]
    chat_id: ChatId
    message_id: MessageId
    text: Annotated[str, Field(min_length=1, max_length=16384)]


class Delete(ActionBase):
    operation: Literal["delete"]
    chat_id: ChatId
    message_id: MessageId


class Forward(ActionBase):
    operation: Literal["forward"]
    chat_id: ChatId  # destination, in the same account
    source_chat_id: ChatId
    message_id: MessageId


class SetTitle(ActionBase):
    operation: Literal["set_title"]
    chat_id: ChatId
    title: Annotated[str, Field(min_length=1, max_length=256)]


class SetMemberStatus(ActionBase):
    operation: Literal["set_member_status"]
    chat_id: ChatId
    member_id: ChatId
    status: Literal["member", "restricted", "banned", "administrator"]


Action = Annotated[
    Union[
        ListChats,
        Search,
        History,
        GetMessage,
        Send,
        SendMedia,
        Edit,
        Delete,
        Forward,
        SetTitle,
        SetMemberStatus,
    ],
    Field(discriminator="operation"),
]
MUTATIONS = {"send", "send_media", "edit", "delete", "forward", "set_title", "set_member_status"}


class Batch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: Annotated[list[Action], Field(min_length=1)]
    idempotency_key: UUID | None = None

    @model_validator(mode="after")
    def mutation_key_required(self):
        if (
            any(action.operation in MUTATIONS for action in self.actions)
            and self.idempotency_key is None
        ):
            raise ValueError("a unique idempotency_key is required for mutation batches")
        return self
