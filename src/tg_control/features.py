"""Typed, account-scoped Telegram feature actions for the shared Gateway Core.

This module describes offline/synthetic domain actions. It does not connect to
Telegram; the live adapter remains separately fail-closed behind the compliance
boundary. Each operation owns a strict parameter model and a permission class so
HTTP, MCP, CLI, and Core use one schema and authorization path.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import date, datetime
from typing import Annotated, Literal, Union
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator, model_validator

from .policy import ChatId, Permission


class FeatureParams(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmptyParams(FeatureParams):
    pass


MessageId = Annotated[int, Field(strict=True, ge=1)]
SmallId = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
ShortText = Annotated[str, Field(min_length=1, max_length=4096)]


class MessageRefParams(FeatureParams):
    message_id: MessageId


class MessagePageParams(FeatureParams):
    message_id: MessageId
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


class ReadMarkerParams(FeatureParams):
    up_to_id: MessageId


class DraftParams(FeatureParams):
    text: Annotated[str, Field(max_length=16384)]
    reply_to_message_id: MessageId | None = None


class ScheduledCreateParams(FeatureParams):
    text: Annotated[str, Field(min_length=1, max_length=16384)]
    run_at: datetime

    @field_validator("run_at")
    @classmethod
    def aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("run_at must include a timezone offset")
        return value


class ScheduledRefParams(FeatureParams):
    scheduled_id: SmallId


class FeatureMediaParams(FeatureParams):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    content_type: Annotated[str, Field(min_length=1, max_length=127)]
    data_base64: Annotated[str, Field(max_length=1_400_000)]

    @field_validator("data_base64")
    @classmethod
    def valid_base64(cls, value: str) -> str:
        try:
            data = base64.b64decode(value, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("invalid base64") from exc
        if len(data) > 1_048_576:
            raise ValueError("synthetic media exceeds 1 MiB")
        return value


class AlbumParams(FeatureParams):
    items: Annotated[list[FeatureMediaParams], Field(min_length=2, max_length=10)]
    caption: Annotated[str, Field(max_length=4096)] = ""


class RichMessageParams(FeatureParams):
    kind: Literal["location", "live_location", "venue", "contact", "dice"]
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    title: Annotated[str, Field(max_length=256)] | None = None
    address: Annotated[str, Field(max_length=512)] | None = None
    phone: Annotated[str, Field(max_length=32)] | None = None
    first_name: Annotated[str, Field(max_length=128)] | None = None
    last_name: Annotated[str, Field(max_length=128)] | None = None
    emoji: Annotated[str, Field(max_length=16)] | None = None

    @model_validator(mode="after")
    def kind_fields(self):
        if self.kind in {"location", "live_location", "venue"} and (
            self.latitude is None or self.longitude is None
        ):
            raise ValueError("location messages require latitude and longitude")
        if self.kind == "venue" and (not self.title or not self.address):
            raise ValueError("venue messages require title and address")
        if self.kind == "contact" and (not self.phone or not self.first_name):
            raise ValueError("contact messages require phone and first_name")
        if self.kind == "dice" and self.emoji not in {"🎲", "🎯", "🏀", "⚽", "🎳", "🎰"}:
            raise ValueError("unsupported dice emoji")
        return self


class ReactionParams(FeatureParams):
    message_id: MessageId
    reaction: Annotated[str, Field(min_length=1, max_length=32)]
    paid: bool = False
    remove: bool = False


class DefaultReactionParams(FeatureParams):
    reaction: Annotated[str, Field(min_length=1, max_length=32)]


class PollCreateParams(FeatureParams):
    question: Annotated[str, Field(min_length=1, max_length=300)]
    options: Annotated[list[ShortText], Field(min_length=2, max_length=10)]
    multiple_answers: bool = False
    anonymous: bool = True

    @field_validator("options")
    @classmethod
    def unique_options(cls, value: list[str]) -> list[str]:
        if len({item.casefold() for item in value}) != len(value):
            raise ValueError("poll options must be unique")
        return value


class PollVoteParams(FeatureParams):
    message_id: MessageId
    option_indexes: Annotated[list[int], Field(min_length=1, max_length=10)]

    @field_validator("option_indexes")
    @classmethod
    def unique_indexes(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value) or any(
            type(item) is not int or item < 0 for item in value
        ):
            raise ValueError("poll option indexes are invalid")
        return value


class StickerQueryParams(FeatureParams):
    query: Annotated[str, Field(max_length=128)] = ""


class StickerSendParams(FeatureParams):
    set_id: SmallId
    sticker_id: SmallId
    emoji: Annotated[str, Field(max_length=16)] | None = None


class ButtonParams(FeatureParams):
    message_id: MessageId
    button_id: SmallId | None = None


class MemberListParams(FeatureParams):
    limit: Annotated[int, Field(strict=True, ge=1, le=200)] = 100


class MemberParams(FeatureParams):
    user_id: SmallId


class GroupCreateParams(FeatureParams):
    title: Annotated[str, Field(min_length=1, max_length=256)]
    member_ids: Annotated[list[SmallId], Field(max_length=100)] = Field(default_factory=list)


class InviteLinkCreateParams(FeatureParams):
    name: Annotated[str, Field(max_length=64)] = ""
    expire_at: datetime | None = None
    member_limit: Annotated[int, Field(strict=True, ge=1, le=100000)] | None = None

    @field_validator("expire_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expire_at must include a timezone offset")
        return value


class InviteLinkRefParams(FeatureParams):
    invite_id: SmallId


class MemberRoleParams(FeatureParams):
    user_id: SmallId
    role: Literal["member", "restricted", "administrator", "owner"]


class ChatPermissionsParams(FeatureParams):
    can_send_messages: bool | None = None
    can_send_media: bool | None = None
    can_add_members: bool | None = None
    can_pin_messages: bool | None = None
    can_change_info: bool | None = None

    @model_validator(mode="after")
    def at_least_one_permission(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("at least one chat permission must be provided")
        return self


class SlowModeParams(FeatureParams):
    seconds: Annotated[int, Field(strict=True, ge=0, le=21600)]


class TopicCreateParams(FeatureParams):
    title: Annotated[str, Field(min_length=1, max_length=128)]
    icon_emoji_id: SmallId | None = None


class TopicRefParams(FeatureParams):
    topic_id: SmallId
    title: Annotated[str, Field(min_length=1, max_length=128)] | None = None


class TopicEditParams(FeatureParams):
    topic_id: SmallId
    title: Annotated[str, Field(min_length=1, max_length=128)]


class TopicUnreadParams(FeatureParams):
    topic_id: SmallId


class PinMessageParams(FeatureParams):
    message_id: MessageId
    notify_members: bool = False


class ChatStateParams(FeatureParams):
    field: Literal["muted", "archived", "pinned", "unread"]
    value: bool


class AllowedReactionsParams(FeatureParams):
    reactions: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=32)]], Field(max_length=50)
    ]


class ContactParams(FeatureParams):
    user_id: SmallId
    first_name: Annotated[str, Field(max_length=128)] | None = None
    last_name: Annotated[str, Field(max_length=128)] | None = None


class ProfileUpdateParams(FeatureParams):
    first_name: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    last_name: Annotated[str, Field(max_length=128)] | None = None
    username: Annotated[str, Field(max_length=32, pattern=r"^[A-Za-z0-9_]*$")] | None = None
    bio: Annotated[str, Field(max_length=512)] | None = None

    @model_validator(mode="after")
    def non_empty_update(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("at least one profile field is required")
        return self


class PrivacyUpdateParams(FeatureParams):
    settings: dict[
        Literal["last_seen", "phone_number", "profile_photo", "calls", "groups", "forwarding"],
        Literal["everyone", "contacts", "nobody"],
    ] = Field(min_length=1, max_length=8)


class SessionRefParams(FeatureParams):
    session_id: SmallId


class StoryCreateParams(FeatureParams):
    content: Annotated[str, Field(min_length=1, max_length=4096)]
    expires_in_hours: Annotated[int, Field(strict=True, ge=1, le=48)] = 24
    privacy: Literal["everyone", "contacts", "close_friends", "selected"] = "contacts"


class StoryRefParams(FeatureParams):
    story_id: SmallId


class StoryUpdateParams(FeatureParams):
    story_id: SmallId
    content: Annotated[str, Field(min_length=1, max_length=4096)]


class StoryStealthParams(FeatureParams):
    enabled: bool


class StoryPageParams(FeatureParams):
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


class StoryStatsParams(FeatureParams):
    story_id: SmallId


class QuickReplyParams(FeatureParams):
    shortcut: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]
    text: Annotated[str, Field(min_length=1, max_length=4096)]


class QuickReplyRefParams(FeatureParams):
    shortcut: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_-]+$")]


class InlineQueryParams(FeatureParams):
    bot_username: Annotated[str, Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_]+$")]
    query: Annotated[str, Field(max_length=256)] = ""


class InlineResultParams(FeatureParams):
    result_id: SmallId
    text: Annotated[str, Field(min_length=1, max_length=4096)]


class WebPreviewParams(FeatureParams):
    message_id: MessageId


class FactCheckParams(FeatureParams):
    message_id: MessageId
    text: Annotated[str, Field(min_length=1, max_length=1024)] | None = None


class FactCheckEditParams(FeatureParams):
    message_id: MessageId
    text: Annotated[str, Field(min_length=1, max_length=1024)]


class FolderParams(FeatureParams):
    folder_id: SmallId | None = None
    title: Annotated[str, Field(min_length=1, max_length=64)]
    chat_ids: Annotated[list[ChatId], Field(max_length=100)] = Field(default_factory=list)
    excluded_chat_ids: Annotated[list[ChatId], Field(max_length=100)] = Field(default_factory=list)
    include_muted: bool | None = None
    include_read: bool | None = None
    include_archived: bool | None = None

    @model_validator(mode="after")
    def distinct_chat_lists(self):
        if len(set(self.chat_ids)) != len(self.chat_ids):
            raise ValueError("folder chat IDs must be unique")
        if len(set(self.excluded_chat_ids)) != len(self.excluded_chat_ids):
            raise ValueError("excluded folder chat IDs must be unique")
        if set(self.chat_ids) & set(self.excluded_chat_ids):
            raise ValueError("a chat cannot be both included and excluded")
        return self


class FolderRefParams(FeatureParams):
    folder_id: SmallId


class FolderOrderParams(FeatureParams):
    folder_ids: Annotated[list[SmallId], Field(min_length=1, max_length=100)]

    @field_validator("folder_ids")
    @classmethod
    def unique_folder_ids(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("folder IDs must be unique")
        return value


class ChannelSettingsParams(FeatureParams):
    signatures_enabled: bool | None = None
    anti_spam_enabled: bool | None = None
    forum_enabled: bool | None = None
    prehistory_enabled: bool | None = None

    @model_validator(mode="after")
    def at_least_one_setting(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("at least one channel setting is required")
        return self


class AutoDeleteParams(FeatureParams):
    seconds: Annotated[int, Field(strict=True, ge=0, le=31_536_000)]


class UserReportParams(FeatureParams):
    user_id: SmallId
    reason: Literal["spam", "violence", "copyright", "other"]
    details: Annotated[str, Field(max_length=512)] = ""


class ProfileDetailsParams(FeatureParams):
    emoji_status: SmallId | None = None
    profile_color: Annotated[int, Field(strict=True, ge=0, le=0xFFFFFF)] | None = None
    birthday: date | None = None
    personal_channel_id: ChatId | None = None

    @model_validator(mode="after")
    def non_empty_update(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("at least one profile detail is required")
        return self


class ReactionPrivacyParams(FeatureParams):
    show_reactions: bool


class StoryActionParams(FeatureParams):
    story_id: SmallId


class StoryReactionParams(FeatureParams):
    story_id: SmallId
    reaction: Annotated[str, Field(min_length=1, max_length=32)]


class StoryReportParams(FeatureParams):
    story_id: SmallId
    reason: Literal["spam", "violence", "copyright", "other"]
    details: Annotated[str, Field(max_length=512)] = ""


class BusinessLinkParams(FeatureParams):
    title: Annotated[str, Field(min_length=1, max_length=64)]
    message: Annotated[str, Field(max_length=512)] = ""
    button_text: Annotated[str, Field(max_length=32)] = ""
    url: Annotated[str, Field(max_length=2048)] | None = None


class BusinessLinkUpdateParams(FeatureParams):
    link_id: SmallId
    title: Annotated[str, Field(min_length=1, max_length=64)]
    message: Annotated[str, Field(max_length=512)] = ""
    button_text: Annotated[str, Field(max_length=32)] = ""
    url: Annotated[str, Field(max_length=2048)] | None = None


class BusinessLinkRefParams(FeatureParams):
    link_id: SmallId


class BusinessLinkResolveParams(FeatureParams):
    slug: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class AudioRefParams(FeatureParams):
    message_id: MessageId


class StarTransactionParams(FeatureParams):
    limit: Annotated[int, Field(strict=True, ge=1, le=100)] = 50


class FinancialActionParams(FeatureParams):
    # Identifier only: no payment credentials, amount, or card data may enter this API.
    item_id: SmallId


@dataclass(frozen=True)
class FeatureSpec:
    operation: str
    group: str
    permission: Permission
    chat_required: bool
    mutation: bool
    params_model: type[FeatureParams]
    description: str
    platform_dependency: str | None = None
    blocked_reason: str | None = None
    conditional_blockers: tuple[tuple[str, str], ...] = ()


class BusinessSettingsParams(FeatureParams):
    enabled: bool | None = None
    greeting_enabled: bool | None = None
    away_enabled: bool | None = None
    intro_enabled: bool | None = None
    business_hours: Annotated[str, Field(max_length=256)] | None = None
    location_label: Annotated[str, Field(max_length=256)] | None = None

    @model_validator(mode="after")
    def non_empty_update(self):
        if all(value is None for value in self.model_dump().values()):
            raise ValueError("at least one Business setting is required")
        return self


# Parameter schemas are deliberately explicit and extra fields are rejected.
# User-message contents are carried only as data and never interpreted.
_FEATURE_ROWS = (
    (
        "get_chat_info",
        "messaging",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read normalized chat metadata.",
    ),
    (
        "get_replies",
        "messaging",
        Permission.READ,
        True,
        False,
        MessagePageParams,
        "Read replies to a message.",
    ),
    (
        "get_permalink",
        "messaging",
        Permission.READ,
        True,
        False,
        MessageRefParams,
        "Return a permalink only when the adapter can provide one.",
    ),
    (
        "get_unread",
        "messaging",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read unread, mention, and reaction counts.",
    ),
    (
        "mark_read",
        "messaging",
        Permission.WRITE,
        True,
        True,
        ReadMarkerParams,
        "Mark messages read in one chat.",
    ),
    (
        "send_typing",
        "messaging",
        Permission.WRITE,
        True,
        True,
        EmptyParams,
        "Set a bounded, transient typing indicator.",
    ),
    (
        "get_draft",
        "messaging",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read the selected chat draft.",
    ),
    (
        "set_draft",
        "messaging",
        Permission.WRITE,
        True,
        True,
        DraftParams,
        "Create or replace an explicit draft.",
    ),
    (
        "clear_draft",
        "messaging",
        Permission.WRITE,
        True,
        True,
        EmptyParams,
        "Clear the selected chat draft.",
    ),
    (
        "list_scheduled_messages",
        "messaging",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "List native scheduled messages for a chat.",
    ),
    (
        "schedule_message",
        "messaging",
        Permission.WRITE,
        True,
        True,
        ScheduledCreateParams,
        "Create a native scheduled message when supported.",
    ),
    (
        "delete_scheduled_message",
        "messaging",
        Permission.WRITE,
        True,
        True,
        ScheduledRefParams,
        "Delete a native scheduled message.",
    ),
    (
        "send_album",
        "media",
        Permission.WRITE,
        True,
        True,
        AlbumParams,
        "Send a synthetic grouped-media album.",
    ),
    (
        "send_rich_message",
        "media",
        Permission.WRITE,
        True,
        True,
        RichMessageParams,
        "Send a location, venue, contact, or dice message.",
    ),
    (
        "get_reactions",
        "interactive",
        Permission.READ,
        True,
        False,
        MessageRefParams,
        "Read reactions for a message.",
    ),
    (
        "set_reaction",
        "interactive",
        Permission.WRITE,
        True,
        True,
        ReactionParams,
        "Add or remove a reaction; paid reactions require separate owner approval.",
        None,
        None,
        (("paid", "approval_required"),),
    ),
    (
        "get_default_reactions",
        "interactive",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "Read default, recent, and top reactions.",
    ),
    (
        "set_default_reaction",
        "interactive",
        Permission.WRITE,
        False,
        True,
        DefaultReactionParams,
        "Set the account default reaction.",
    ),
    (
        "create_poll",
        "interactive",
        Permission.WRITE,
        True,
        True,
        PollCreateParams,
        "Create a poll in one chat.",
    ),
    (
        "get_poll",
        "interactive",
        Permission.READ,
        True,
        False,
        MessageRefParams,
        "Read poll results; voter visibility is platform-limited.",
    ),
    (
        "vote_poll",
        "interactive",
        Permission.WRITE,
        True,
        True,
        PollVoteParams,
        "Submit poll vote choices.",
    ),
    (
        "close_poll",
        "interactive",
        Permission.WRITE,
        True,
        True,
        MessageRefParams,
        "Close a poll owned by this account.",
    ),
    (
        "list_sticker_sets",
        "interactive",
        Permission.READ,
        False,
        False,
        StickerQueryParams,
        "Search synthetic sticker-set fixtures.",
    ),
    (
        "send_sticker",
        "interactive",
        Permission.WRITE,
        True,
        True,
        StickerSendParams,
        "Send a sticker from an available set.",
    ),
    (
        "get_buttons",
        "interactive",
        Permission.READ,
        True,
        False,
        MessageRefParams,
        "Read supported message button metadata.",
    ),
    (
        "press_button",
        "interactive",
        Permission.WRITE,
        True,
        True,
        ButtonParams,
        "Invoke a supported callback button without fetching URLs.",
    ),
    (
        "get_members",
        "groups",
        Permission.READ,
        True,
        False,
        MemberListParams,
        "List visible members and normalized roles.",
    ),
    (
        "join_chat",
        "groups",
        Permission.WRITE,
        True,
        True,
        EmptyParams,
        "Join an eligible public chat.",
    ),
    ("leave_chat", "groups", Permission.WRITE, True, True, EmptyParams, "Leave a chat."),
    (
        "create_group",
        "groups",
        Permission.ADMIN,
        False,
        True,
        GroupCreateParams,
        "Create a synthetic group under account Admin policy.",
    ),
    (
        "invite_user",
        "groups",
        Permission.ADMIN,
        True,
        True,
        MemberParams,
        "Invite one user where rights allow.",
    ),
    (
        "list_invite_links",
        "groups",
        Permission.ADMIN,
        True,
        False,
        EmptyParams,
        "List invite links where rights allow.",
    ),
    (
        "create_invite_link",
        "groups",
        Permission.ADMIN,
        True,
        True,
        InviteLinkCreateParams,
        "Create an invite link where rights allow.",
    ),
    (
        "revoke_invite_link",
        "groups",
        Permission.ADMIN,
        True,
        True,
        InviteLinkRefParams,
        "Revoke an invite link.",
    ),
    (
        "ban_member",
        "groups",
        Permission.ADMIN,
        True,
        True,
        MemberParams,
        "Ban a member where rights allow.",
    ),
    (
        "unban_member",
        "groups",
        Permission.ADMIN,
        True,
        True,
        MemberParams,
        "Unban a member where rights allow.",
    ),
    (
        "set_member_role",
        "groups",
        Permission.ADMIN,
        True,
        True,
        MemberRoleParams,
        "Set a member role where rights allow.",
    ),
    (
        "set_chat_permissions",
        "groups",
        Permission.ADMIN,
        True,
        True,
        ChatPermissionsParams,
        "Update chat permissions where rights allow.",
    ),
    (
        "set_slow_mode",
        "groups",
        Permission.ADMIN,
        True,
        True,
        SlowModeParams,
        "Set slow mode where rights allow.",
    ),
    (
        "get_admin_log",
        "groups",
        Permission.ADMIN,
        True,
        False,
        MemberListParams,
        "Read the bounded synthetic admin log.",
    ),
    (
        "list_topics",
        "groups",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "List forum topics where available.",
    ),
    (
        "get_topic_unread",
        "groups",
        Permission.READ,
        True,
        False,
        TopicUnreadParams,
        "Read unread count for one forum topic.",
    ),
    (
        "create_topic",
        "groups",
        Permission.ADMIN,
        True,
        True,
        TopicCreateParams,
        "Create a forum topic where available.",
    ),
    (
        "edit_topic",
        "groups",
        Permission.ADMIN,
        True,
        True,
        TopicEditParams,
        "Edit a forum topic where available.",
    ),
    (
        "delete_topic",
        "groups",
        Permission.ADMIN,
        True,
        True,
        TopicUnreadParams,
        "Delete a forum topic where available.",
    ),
    (
        "pin_message",
        "groups",
        Permission.ADMIN,
        True,
        True,
        PinMessageParams,
        "Pin a message where rights allow.",
    ),
    (
        "unpin_message",
        "groups",
        Permission.ADMIN,
        True,
        True,
        MessageRefParams,
        "Unpin a message where rights allow.",
    ),
    (
        "get_chat_state",
        "groups",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read mute, archive, pin, and unread settings.",
    ),
    (
        "set_chat_state",
        "groups",
        Permission.WRITE,
        True,
        True,
        ChatStateParams,
        "Change personal mute/archive/pin/unread state.",
    ),
    (
        "set_allowed_reactions",
        "groups",
        Permission.ADMIN,
        True,
        True,
        AllowedReactionsParams,
        "Configure chat-allowed reactions where supported.",
    ),
    (
        "list_contacts",
        "contacts",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "List synthetic contacts.",
    ),
    (
        "add_contact",
        "contacts",
        Permission.WRITE,
        False,
        True,
        ContactParams,
        "Add a contact by account-scoped user ID.",
    ),
    (
        "remove_contact",
        "contacts",
        Permission.WRITE,
        False,
        True,
        MemberParams,
        "Remove an account-scoped contact.",
    ),
    (
        "list_contact_requests",
        "contacts",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "List pending contact requests.",
    ),
    (
        "accept_contact_request",
        "contacts",
        Permission.WRITE,
        False,
        True,
        MemberParams,
        "Accept a pending contact request.",
    ),
    (
        "block_user",
        "contacts",
        Permission.WRITE,
        False,
        True,
        MemberParams,
        "Block one account-scoped user.",
    ),
    (
        "unblock_user",
        "contacts",
        Permission.WRITE,
        False,
        True,
        MemberParams,
        "Unblock one account-scoped user.",
    ),
    (
        "get_profile",
        "account_settings",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "Read synthetic profile metadata.",
    ),
    (
        "update_profile",
        "account_settings",
        Permission.WRITE,
        False,
        True,
        ProfileUpdateParams,
        "Update allowed profile fields; phone and credentials are excluded.",
    ),
    (
        "get_privacy",
        "account_settings",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "Read supported privacy settings.",
    ),
    (
        "update_privacy",
        "account_settings",
        Permission.WRITE,
        False,
        True,
        PrivacyUpdateParams,
        "Update supported privacy rules.",
    ),
    (
        "list_sessions",
        "account_settings",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "List normalized session metadata without secrets.",
    ),
    (
        "terminate_session",
        "account_settings",
        Permission.ADMIN,
        False,
        True,
        SessionRefParams,
        "Terminate a synthetic session by ID.",
    ),
    (
        "list_stories",
        "stories_advanced",
        Permission.READ,
        True,
        False,
        StoryPageParams,
        "List account stories visible in the selected chat.",
    ),
    (
        "get_story",
        "stories_advanced",
        Permission.READ,
        True,
        False,
        StoryRefParams,
        "Read one story visible in the selected chat.",
    ),
    (
        "create_story",
        "stories_advanced",
        Permission.WRITE,
        False,
        True,
        StoryCreateParams,
        "Publish a synthetic story; real publication is gated.",
    ),
    (
        "update_story",
        "stories_advanced",
        Permission.WRITE,
        False,
        True,
        StoryUpdateParams,
        "Edit a synthetic story; real editing is gated.",
    ),
    (
        "delete_story",
        "stories_advanced",
        Permission.WRITE,
        False,
        True,
        StoryStatsParams,
        "Delete a synthetic story; real deletion is gated.",
    ),
    (
        "get_story_stats",
        "stories_advanced",
        Permission.READ,
        False,
        False,
        StoryStatsParams,
        "Read synthetic story interaction counts.",
    ),
    (
        "get_discussion",
        "stories_advanced",
        Permission.READ,
        True,
        False,
        MessageRefParams,
        "Resolve a synthetic discussion chat for a post.",
    ),
    (
        "get_stats",
        "stories_advanced",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read bounded synthetic chat statistics.",
    ),
    (
        "get_boost_status",
        "stories_advanced",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read boost data when the account/platform exposes it.",
        "boosts",
    ),
    (
        "get_business_settings",
        "business_stars",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "Read Business settings when the account is eligible.",
        "business",
    ),
    (
        "update_business_settings",
        "business_stars",
        Permission.WRITE,
        False,
        True,
        BusinessSettingsParams,
        "Update Business settings when eligible.",
        "business",
    ),
    (
        "get_stars",
        "business_stars",
        Permission.ADMIN,
        False,
        False,
        EmptyParams,
        "Read Stars balance/transactions when exposed.",
        "stars",
    ),
    (
        "list_gifts",
        "business_stars",
        Permission.READ,
        False,
        False,
        StickerQueryParams,
        "Browse gifts when exposed.",
        "stars",
    ),
    (
        "list_quick_replies",
        "business_stars",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "List account quick replies.",
    ),
    (
        "save_quick_reply",
        "business_stars",
        Permission.WRITE,
        False,
        True,
        QuickReplyParams,
        "Create or update a quick reply.",
    ),
    (
        "delete_quick_reply",
        "business_stars",
        Permission.WRITE,
        False,
        True,
        QuickReplyRefParams,
        "Delete a quick reply.",
    ),
    (
        "get_group_call",
        "business_stars",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read group-call metadata when exposed.",
        "group_calls",
    ),
    (
        "query_inline_bot",
        "interactive",
        Permission.WRITE,
        False,
        True,
        InlineQueryParams,
        "Query a named inline bot without executing returned content.",
    ),
    (
        "send_inline_result",
        "interactive",
        Permission.WRITE,
        True,
        True,
        InlineResultParams,
        "Send a previously selected synthetic inline result.",
    ),
    (
        "get_web_page_preview",
        "media",
        Permission.READ,
        True,
        False,
        WebPreviewParams,
        "Read an already-available preview; no URL is fetched.",
    ),
    (
        "get_web_app",
        "interactive",
        Permission.READ,
        True,
        False,
        ButtonParams,
        "Read safe Web App metadata; does not execute the app.",
    ),
    (
        "get_fact_check",
        "stories_advanced",
        Permission.READ,
        True,
        False,
        FactCheckParams,
        "Read a fact-check only when exposed by the official adapter.",
        "fact_check",
    ),
    (
        "translate_message",
        "messaging",
        Permission.READ,
        True,
        False,
        MessageRefParams,
        "Translation is disabled until a legal/provider task is approved.",
        "translation",
    ),
    (
        "get_profile_photo",
        "media",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "Read profile-photo metadata where available.",
    ),
    (
        "set_profile_photo",
        "media",
        Permission.WRITE,
        False,
        True,
        FeatureMediaParams,
        "Set a profile photo through the account media path.",
    ),
    (
        "delete_profile_photo",
        "media",
        Permission.WRITE,
        False,
        True,
        EmptyParams,
        "Delete the current synthetic profile photo.",
    ),
    (
        "send_voice",
        "media",
        Permission.WRITE,
        True,
        True,
        FeatureMediaParams,
        "Send a validated synthetic voice clip.",
    ),
    (
        "send_video_note",
        "media",
        Permission.WRITE,
        True,
        True,
        FeatureMediaParams,
        "Send a validated synthetic round video note.",
    ),
    (
        "send_quick_reply",
        "business_stars",
        Permission.WRITE,
        True,
        True,
        QuickReplyRefParams,
        "Send a stored quick reply to the selected chat.",
    ),
    (
        "list_chat_folders",
        "groups",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "List account chat-folder metadata.",
    ),
    (
        "save_chat_folder",
        "groups",
        Permission.WRITE,
        False,
        True,
        FolderParams,
        "Create or replace a chat folder with explicit includes/exclusions.",
    ),
    (
        "delete_chat_folder",
        "groups",
        Permission.WRITE,
        False,
        True,
        FolderRefParams,
        "Delete a synthetic chat folder.",
    ),
    (
        "reorder_chat_folders",
        "groups",
        Permission.WRITE,
        False,
        True,
        FolderOrderParams,
        "Reorder known chat folders.",
    ),
    (
        "get_folder_suggestions",
        "groups",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "Read Telegram-provided folder suggestions when exposed.",
    ),
    (
        "get_own_role",
        "groups",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read the synthetic account role and rights in one chat.",
    ),
    (
        "get_channel_settings",
        "groups",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read channel/forum configuration metadata.",
    ),
    (
        "update_channel_settings",
        "groups",
        Permission.ADMIN,
        True,
        True,
        ChannelSettingsParams,
        "Update channel toggles where Telegram rights allow.",
    ),
    (
        "list_join_requests",
        "groups",
        Permission.ADMIN,
        True,
        False,
        MemberListParams,
        "List bounded synthetic pending join requests.",
    ),
    (
        "approve_join_request",
        "groups",
        Permission.ADMIN,
        True,
        True,
        MemberParams,
        "Approve one join request where rights allow.",
    ),
    (
        "decline_join_request",
        "groups",
        Permission.ADMIN,
        True,
        True,
        MemberParams,
        "Decline one join request where rights allow.",
    ),
    (
        "get_auto_delete_timer",
        "messaging",
        Permission.READ,
        True,
        False,
        EmptyParams,
        "Read the synthetic chat auto-delete timer.",
    ),
    (
        "set_auto_delete_timer",
        "messaging",
        Permission.ADMIN,
        True,
        True,
        AutoDeleteParams,
        "Set a chat auto-delete timer where rights allow.",
    ),
    (
        "report_user",
        "contacts",
        Permission.ADMIN,
        True,
        True,
        UserReportParams,
        "Record an explicit synthetic report request; never auto-report content.",
    ),
    (
        "update_profile_details",
        "account_settings",
        Permission.WRITE,
        False,
        True,
        ProfileDetailsParams,
        "Update synthetic emoji status, profile color, birthday, or personal channel.",
    ),
    (
        "get_reaction_privacy",
        "account_settings",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "Read synthetic reaction-visibility preference.",
    ),
    (
        "update_reaction_privacy",
        "account_settings",
        Permission.WRITE,
        False,
        True,
        ReactionPrivacyParams,
        "Update synthetic reaction-visibility preference.",
    ),
    (
        "mark_story_read",
        "stories_advanced",
        Permission.WRITE,
        True,
        True,
        StoryActionParams,
        "Mark a visible synthetic story as read.",
    ),
    (
        "react_to_story",
        "stories_advanced",
        Permission.WRITE,
        True,
        True,
        StoryReactionParams,
        "Add a synthetic story reaction.",
    ),
    (
        "pin_story",
        "stories_advanced",
        Permission.WRITE,
        False,
        True,
        StoryActionParams,
        "Pin or unpin an owned synthetic story.",
    ),
    (
        "archive_story",
        "stories_advanced",
        Permission.WRITE,
        False,
        True,
        StoryActionParams,
        "Archive an owned synthetic story.",
    ),
    (
        "set_story_stealth",
        "stories_advanced",
        Permission.WRITE,
        False,
        True,
        StoryStealthParams,
        "Use stealth viewing only when the account is eligible.",
        "premium",
    ),
    (
        "list_story_viewers",
        "stories_advanced",
        Permission.READ,
        True,
        False,
        StoryStatsParams,
        "Read synthetic story-viewer references.",
    ),
    (
        "report_story",
        "stories_advanced",
        Permission.ADMIN,
        True,
        True,
        StoryReportParams,
        "Report a selected story only through an explicitly approved platform path.",
    ),
    (
        "get_outbox_read_date",
        "messaging",
        Permission.READ,
        True,
        False,
        MessageRefParams,
        "Read message read-date metadata where Telegram exposes it.",
    ),
    (
        "get_read_participants",
        "messaging",
        Permission.READ,
        True,
        False,
        MessageRefParams,
        "Read visible read participants where Telegram exposes them.",
    ),
    (
        "list_discussion_eligible_chats",
        "stories_advanced",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "List synthetic chats eligible for channel discussion.",
    ),
    (
        "get_boosters",
        "stories_advanced",
        Permission.READ,
        True,
        False,
        MemberListParams,
        "List boosters only when the account/platform exposes them.",
        "boosts",
    ),
    (
        "get_group_call_participants",
        "business_stars",
        Permission.READ,
        True,
        False,
        MemberListParams,
        "Read group-call participants when exposed.",
        "group_calls",
    ),
    (
        "list_business_links",
        "business_stars",
        Permission.READ,
        False,
        False,
        EmptyParams,
        "List Business chat links for an eligible account.",
        "business",
    ),
    (
        "create_business_link",
        "business_stars",
        Permission.WRITE,
        False,
        True,
        BusinessLinkParams,
        "Create a Business chat link for an eligible account.",
        "business",
    ),
    (
        "update_business_link",
        "business_stars",
        Permission.WRITE,
        False,
        True,
        BusinessLinkUpdateParams,
        "Update a Business chat link for an eligible account.",
        "business",
    ),
    (
        "delete_business_link",
        "business_stars",
        Permission.WRITE,
        False,
        True,
        BusinessLinkRefParams,
        "Delete a Business chat link for an eligible account.",
        "business",
    ),
    (
        "resolve_business_link",
        "business_stars",
        Permission.READ,
        False,
        False,
        BusinessLinkResolveParams,
        "Resolve a known synthetic Business link slug.",
        "business",
    ),
    (
        "get_star_transactions",
        "business_stars",
        Permission.ADMIN,
        False,
        False,
        StarTransactionParams,
        "Read Stars transaction metadata when exposed.",
        "stars",
    ),
    (
        "get_star_topup_options",
        "business_stars",
        Permission.ADMIN,
        False,
        False,
        EmptyParams,
        "Read available Stars top-up options without purchasing.",
        "stars",
    ),
    (
        "list_star_subscriptions",
        "business_stars",
        Permission.ADMIN,
        False,
        False,
        EmptyParams,
        "Read Stars subscription metadata when exposed.",
        "stars",
    ),
    (
        "save_gift",
        "business_stars",
        Permission.WRITE,
        False,
        True,
        FinancialActionParams,
        "Save an eligible synthetic gift without converting or spending Stars.",
        "stars",
    ),
    (
        "convert_gift",
        "business_stars",
        Permission.ADMIN,
        False,
        True,
        FinancialActionParams,
        "Gift conversion may consume value and is blocked pending explicit owner approval.",
        "stars",
        "Financial or irreversible Telegram action requires separate owner approval.",
    ),
    (
        "manage_star_subscription",
        "business_stars",
        Permission.ADMIN,
        False,
        True,
        FinancialActionParams,
        "Stars subscription changes are blocked pending explicit owner approval.",
        "stars",
        "Financial Telegram action requires separate owner approval.",
    ),
    (
        "transcribe_audio",
        "media",
        Permission.READ,
        True,
        False,
        AudioRefParams,
        "Telegram-native transcription is platform/account-dependent; no external AI is used.",
        "premium",
    ),
    (
        "edit_fact_check",
        "stories_advanced",
        Permission.ADMIN,
        True,
        True,
        FactCheckEditParams,
        "Edit a fact-check only through a supported official method.",
        "fact_check",
    ),
    (
        "delete_fact_check",
        "stories_advanced",
        Permission.ADMIN,
        True,
        True,
        MessageRefParams,
        "Delete a fact-check only through a supported official method.",
        "fact_check",
    ),
)

FEATURE_SPECS: dict[str, FeatureSpec] = {
    row[0]: FeatureSpec(
        operation=row[0],
        group=row[1],
        permission=row[2],
        chat_required=row[3],
        mutation=row[4],
        params_model=row[5],
        description=row[6],
        platform_dependency=row[7] if len(row) > 7 else None,
        blocked_reason=row[8] if len(row) > 8 else None,
        conditional_blockers=row[9] if len(row) > 9 else (),
    )
    for row in _FEATURE_ROWS
}

FEATURE_ACTION_MODELS: dict[str, type[BaseModel]] = {}
for _operation, _spec in FEATURE_SPECS.items():
    _fields: dict[str, tuple[object, object]] = {
        "account_id": (UUID, ...),
        "operation": (Literal[_operation], ...),
        "params": (_spec.params_model, ...),
    }
    if _spec.chat_required:
        _fields["chat_id"] = (ChatId, ...)
    FEATURE_ACTION_MODELS[_operation] = create_model(
        f"FeatureAction_{_operation}",
        __config__=ConfigDict(extra="forbid"),
        __module__=__name__,
        **_fields,
    )

FeatureAction = Annotated[
    Union[tuple(FEATURE_ACTION_MODELS.values())], Field(discriminator="operation")
]
FEATURE_MUTATIONS = frozenset(
    operation for operation, spec in FEATURE_SPECS.items() if spec.mutation
)
FEATURE_OPERATION_NAMES = frozenset(FEATURE_SPECS)


# The exact allow-list of synthetic feature operations is also used by the
# capability response. A missing handler is a typed adapter failure, never a
# fabricated success.
SYNTHETIC_FEATURE_OPERATIONS = frozenset(
    {
        "get_chat_info",
        "get_replies",
        "get_permalink",
        "get_unread",
        "mark_read",
        "send_typing",
        "get_draft",
        "set_draft",
        "clear_draft",
        "list_scheduled_messages",
        "schedule_message",
        "delete_scheduled_message",
        "send_album",
        "send_rich_message",
        "get_reactions",
        "set_reaction",
        "get_default_reactions",
        "set_default_reaction",
        "create_poll",
        "get_poll",
        "vote_poll",
        "close_poll",
        "list_sticker_sets",
        "send_sticker",
        "get_buttons",
        "press_button",
        "get_members",
        "join_chat",
        "leave_chat",
        "create_group",
        "invite_user",
        "list_invite_links",
        "create_invite_link",
        "revoke_invite_link",
        "ban_member",
        "unban_member",
        "set_member_role",
        "set_chat_permissions",
        "set_slow_mode",
        "get_admin_log",
        "list_topics",
        "get_topic_unread",
        "create_topic",
        "edit_topic",
        "delete_topic",
        "pin_message",
        "unpin_message",
        "get_chat_state",
        "set_chat_state",
        "set_allowed_reactions",
        "list_contacts",
        "add_contact",
        "remove_contact",
        "list_contact_requests",
        "accept_contact_request",
        "block_user",
        "unblock_user",
        "get_profile",
        "update_profile",
        "get_privacy",
        "update_privacy",
        "list_sessions",
        "terminate_session",
        "list_stories",
        "get_story",
        "create_story",
        "update_story",
        "delete_story",
        "get_story_stats",
        "get_discussion",
        "get_stats",
        "list_quick_replies",
        "save_quick_reply",
        "delete_quick_reply",
        "query_inline_bot",
        "send_inline_result",
        "get_web_page_preview",
        "get_web_app",
        "get_profile_photo",
        "set_profile_photo",
        "delete_profile_photo",
        "send_voice",
        "send_video_note",
        "send_quick_reply",
        "list_chat_folders",
        "save_chat_folder",
        "delete_chat_folder",
        "reorder_chat_folders",
        "get_own_role",
        "get_channel_settings",
        "update_channel_settings",
        "list_join_requests",
        "approve_join_request",
        "decline_join_request",
        "get_auto_delete_timer",
        "set_auto_delete_timer",
        "report_user",
        "update_profile_details",
        "get_reaction_privacy",
        "update_reaction_privacy",
        "mark_story_read",
        "react_to_story",
        "pin_story",
        "archive_story",
        "list_story_viewers",
        "report_story",
        "list_discussion_eligible_chats",
        "get_group_call",
        "get_group_call_participants",
        "list_business_links",
        "create_business_link",
        "update_business_link",
        "delete_business_link",
        "resolve_business_link",
        "get_business_settings",
        "update_business_settings",
        "get_stars",
        "list_gifts",
        "get_star_transactions",
        "get_star_topup_options",
        "list_star_subscriptions",
        "save_gift",
        "get_boost_status",
        "get_boosters",
    }
)
