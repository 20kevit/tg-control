"""Offline-only, conservative TDJSON read screening for synthetic fixtures.

No adapter imports this module and it performs no I/O. Passing these structural
checks does not grant permission to process content: the unresolved compliance
and owner gates, authentication, per-request policy, and fresh native metadata
remain mandatory for any future real adapter.
"""

from __future__ import annotations

import re
from typing import Any

from .base import ChatKind

_USERNAME = re.compile(r"[A-Za-z0-9_]{5,32}\Z", re.ASCII)


def _identifier(value: Any, *, positive: bool = False) -> bool:
    return type(value) is int and 0 < abs(value) <= 2**53 - 1 and (not positive or value > 0)


def classify_chat(
    chat: dict[str, Any], supergroup: dict[str, Any] | None = None
) -> ChatKind | None:
    """Classify known chat metadata; None means the target is not authorizable.

    Supergroup visibility needs fresh, matching native metadata, including
    location-based public groups. Unknown, malformed, secret and channel-direct
    message group types fail closed. A one-to-one chat always needs an explicit
    private grant, even when its user has a public username.
    """
    if not isinstance(chat, dict) or chat.get("@type") != "chat" or not _identifier(chat.get("id")):
        return None
    kind = chat.get("type")
    if not isinstance(kind, dict):
        return None
    if kind.get("@type") == "chatTypePrivate":
        return ChatKind.PRIVATE_DIRECT if _identifier(kind.get("user_id"), positive=True) else None
    if kind.get("@type") == "chatTypeBasicGroup":
        return (
            ChatKind.PRIVATE_GROUP
            if _identifier(kind.get("basic_group_id"), positive=True)
            else None
        )
    if kind.get("@type") != "chatTypeSupergroup":
        return None
    group_id = kind.get("supergroup_id")
    is_channel = kind.get("is_channel")
    if (
        not _identifier(group_id, positive=True)
        or type(is_channel) is not bool
        or not isinstance(supergroup, dict)
        or supergroup.get("@type") != "supergroup"
        or type(supergroup.get("id")) is not int
        or supergroup["id"] != group_id
        or type(supergroup.get("is_channel")) is not bool
        or supergroup["is_channel"] != is_channel
        or type(supergroup.get("has_location")) is not bool
        or "usernames" not in supergroup
        or supergroup.get("is_direct_messages_group") is not False
        or supergroup.get("is_administered_direct_messages_group") is not False
    ):
        return None
    location = supergroup["has_location"]
    if is_channel and location:
        return None  # Inconsistent metadata; don't infer a grant or an exclusion.
    usernames = supergroup.get("usernames")
    if usernames is None:
        active: list[str] = []
    elif isinstance(usernames, dict) and usernames.get("@type") == "usernames":
        active = usernames.get("active_usernames")
        if not isinstance(active, list) or any(
            not isinstance(name, str) or not _USERNAME.fullmatch(name) for name in active
        ):
            return None
    else:
        return None
    if active or location:
        return ChatKind.PUBLIC_CHANNEL if is_channel else ChatKind.PUBLIC_GROUP
    return ChatKind.PRIVATE_GROUP


def passes_text_safety_screen(
    chat: dict[str, Any], message: dict[str, Any], supergroup: dict[str, Any] | None = None
) -> bool:
    """Conservatively screen known text fields, without copying message content.

    True is neither authorization nor legal clearance and does not cover media,
    updates, policy changes, retention, copies/forwards, or native caching.
    """
    if (
        classify_chat(chat, supergroup) is None
        or chat.get("has_protected_content") is not False
        or type(chat.get("message_auto_delete_time")) is not int
        or chat["message_auto_delete_time"] != 0
        or not isinstance(message, dict)
        or message.get("@type") != "message"
        or not _identifier(message.get("id"))
        or type(message.get("chat_id")) is not int
        or message["chat_id"] != chat["id"]
        or message.get("can_be_saved") is not True
    ):
        return False
    for nullable in (
        "self_destruct_type",
        "ephemeral_content",
        "restriction_info",
        "sending_state",
        "scheduling_state",
    ):
        if nullable not in message or message[nullable] is not None:
            return False
    for timer in ("self_destruct_in", "auto_delete_in"):
        value = message.get(timer)
        if type(value) not in (int, float) or value != 0:
            return False
    if type(message.get("ephemeral_message_id")) is not int or message["ephemeral_message_id"] != 0:
        return False
    content = message.get("content")
    return (
        isinstance(content, dict)
        and content.get("@type") == "messageText"
        and isinstance(content.get("text"), dict)
        and content["text"].get("@type") == "formattedText"
        and isinstance(content["text"].get("text"), str)
    )
