"""Synthetic account simulator. It cannot connect to Telegram."""

from __future__ import annotations

import asyncio
import base64
import binascii
import copy
import hashlib
import time
import uuid
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any, Callable

from ..features import FEATURE_SPECS, SYNTHETIC_FEATURE_OPERATIONS
from .base import AdapterError, Chat, ChatKind, SessionStatus, SessionUnavailable, Update
from .synthetic_store import SyntheticStore


@dataclass
class _Account:
    chats: dict[str, Chat]
    messages: dict[str, list[dict[str, Any]]]
    state: str = "ready"
    next_id: int = 10
    members: dict[str, dict[str, str]] = field(default_factory=dict)
    features: dict[str, Any] = field(default_factory=dict)


def _fixture(account_id: str, timestamp: float) -> _Account:
    chats = {
        "public-channel": Chat("public-channel", ChatKind.PUBLIC_CHANNEL, "Synthetic channel"),
        "public-group": Chat("public-group", ChatKind.PUBLIC_GROUP, "Synthetic public group"),
        "private-group": Chat("private-group", ChatKind.PRIVATE_GROUP, "Synthetic private group"),
        "private-direct": Chat("private-direct", ChatKind.PRIVATE_DIRECT, "Synthetic direct chat"),
        "saved-messages": Chat("saved-messages", ChatKind.PRIVATE_DIRECT, "Saved Messages"),
    }
    messages = {
        chat_id: [
            {
                "id": 1,
                "chat_id": chat_id,
                "text": f"Synthetic fixture {account_id} in {chat_id}",
                "media": None,
            }
        ]
        for chat_id in chats
    }
    data = base64.b64encode(b"synthetic file\n").decode("ascii")
    messages["public-channel"].append(
        {
            "id": 2,
            "chat_id": "public-channel",
            "text": "Synthetic attachment",
            "media": {
                "filename": "sample.txt",
                "content_type": "text/plain",
                "data_base64": data,
                "size": len(base64.b64decode(data)),
                "created_at": timestamp,
            },
        }
    )
    messages["public-group"].append(
        {
            "id": 2,
            "chat_id": "public-group",
            "text": "Synthetic reply fixture",
            "media": None,
            "reply_to_message_id": 1,
        }
    )
    messages["public-group"][0]["buttons"] = [
        {"button_id": "synthetic-callback", "text": "Synthetic callback", "kind": "callback"},
        {
            "button_id": "synthetic-web-app",
            "text": "Synthetic web app",
            "kind": "web_app",
            "domain": "example.invalid",
        },
    ]
    members = {
        "public-group": {"synthetic-user-1": "member", "synthetic-admin": "administrator"},
        "private-group": {"synthetic-user-1": "member"},
    }
    features: dict[str, Any] = {
        "read_up_to": {},
        "drafts": {},
        "scheduled_messages": {},
        "reactions": {},
        "default_reaction": {"reaction": "👍", "recent": ["👍", "❤️"], "top": ["👍"]},
        "polls": {},
        "sticker_sets": [
            {
                "set_id": "synthetic-basic",
                "title": "Synthetic Basics",
                "stickers": ["sticker-wave", "sticker-heart"],
            }
        ],
        "chat_settings": {},
        "channel_settings": {},
        "auto_delete_seconds": {},
        "chat_folders": [],
        "join_requests": {
            "public-group": [
                {"user_id": "synthetic-user-3", "first_name": "Synthetic", "last_name": "Applicant"}
            ]
        },
        "joined": [
            "public-channel",
            "public-group",
            "private-group",
            "private-direct",
            "saved-messages",
        ],
        "invite_links": {},
        "topics": {
            "public-group": [
                {
                    "topic_id": "topic-general",
                    "title": "General",
                    "unread_count": 1,
                    "closed": False,
                }
            ]
        },
        "admin_log": [],
        "chat_permissions": {},
        "allowed_reactions": {},
        "contacts": [
            {"user_id": "synthetic-user-1", "first_name": "Synthetic", "last_name": "Contact"}
        ],
        "contact_requests": [
            {"user_id": "synthetic-user-2", "first_name": "Synthetic", "last_name": "Requester"}
        ],
        "blocked_users": [],
        "profile": {
            "first_name": "Synthetic",
            "last_name": "Account",
            "username": "synthetic_account",
            "bio": "Synthetic fixture only",
            "emoji_status": None,
            "profile_color": None,
            "birthday": None,
            "personal_channel_id": None,
        },
        "privacy": {
            "last_seen": "contacts",
            "phone_number": "nobody",
            "profile_photo": "contacts",
            "calls": "contacts",
            "groups": "contacts",
            "forwarding": "contacts",
        },
        "reaction_privacy": {"show_reactions": True},
        "business_settings": {},
        "business_links": [],
        "stars": {
            "balance": 100,
            "transactions": [],
            "topup_options": [50, 100, 250],
            "subscriptions": [],
        },
        "gifts": [{"gift_id": "synthetic-gift-1", "title": "Synthetic Star Gift", "saved": False}],
        "boosters": [{"user_id": "synthetic-user-1", "boost_count": 1}],
        "group_call": {"active": False, "participant_count": 0, "participants": []},
        "reports": [],
        "read_stories": [],
        "story_reactions": {},
        "pinned_stories": [],
        "archived_stories": [],
        "sessions": [
            {
                "session_id": "synthetic-current",
                "current": True,
                "device": "Synthetic fixture",
                "state": "ready",
            },
            {
                "session_id": "synthetic-secondary",
                "current": False,
                "device": "Synthetic test device",
                "state": "active",
            },
        ],
        "stories": [
            {
                "story_id": "synthetic-story-1",
                "chat_id": "public-group",
                "owner_ref": "synthetic-user-1",
                "content": "Synthetic story fixture",
                "state": "active",
                "views": 3,
                "reactions": 1,
                "viewers": ["synthetic-user-1"],
                "expires_at": timestamp + 86400,
            }
        ],
        "discussion_links": {"public-channel": "public-group"},
        "quick_replies": {"hello": "Synthetic quick reply"},
        "web_previews": {
            "public-channel:2": {"title": "Synthetic preview", "domain": "example.invalid"}
        },
        "profile_photo": None,
        "eligibility": {
            "premium": False,
            "stars": False,
            "business": False,
            "boosts": False,
            "group_calls": False,
            "fact_check": False,
            "translation": False,
        },
        "account_rights": ["create_group", "manage_sessions"],
        "chat_rights": {
            "public-group": [
                "invite_users",
                "manage_invite_links",
                "ban_users",
                "promote_members",
                "change_info",
                "manage_topics",
                "pin_messages",
                "manage_permissions",
                "manage_slow_mode",
                "manage_reactions",
                "view_admin_log",
                "manage_join_requests",
            ],
            "private-group": [
                "invite_users",
                "manage_invite_links",
                "ban_users",
                "promote_members",
                "change_info",
                "manage_topics",
                "pin_messages",
                "manage_permissions",
                "manage_slow_mode",
                "manage_reactions",
                "view_admin_log",
                "manage_join_requests",
            ],
        },
    }
    return _Account(chats, messages, members=members, features=features)


def _metadata(message: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(message)
    media = result.get("media")
    if media is not None:
        media.pop("data_base64", None)
        media.pop("created_at", None)
    return result


def _encode(account: _Account) -> dict:
    return {
        "version": 1,
        "chats": {key: asdict(value) for key, value in account.chats.items()},
        "messages": account.messages,
        "state": account.state,
        "next_id": account.next_id,
        "members": account.members,
        "features": account.features,
    }


def _decode(data: dict) -> _Account:
    chats = {
        key: Chat(value["id"], ChatKind(value["kind"]), value["title"])
        for key, value in data["chats"].items()
    }
    default_features = _fixture("synthetic-upgrade", 0).features
    features = copy.deepcopy(default_features)
    if isinstance(data.get("features"), dict):
        features.update(data["features"])
    return _Account(
        chats,
        data["messages"],
        data.get("state", "ready"),
        data.get("next_id", 10),
        data.get("members", {}),
        features,
    )


class SyntheticAdapter:
    """Synthetic source state; optional per-account encryption, no Telegram/network access."""

    def __init__(
        self, store: SyntheticStore | None = None, *, clock: Callable[[], float] = time.time
    ):
        self._accounts: dict[str, _Account] = {}
        self._faults: dict[tuple[str, str], deque[Exception]] = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._store = store
        self._clock = clock
        self._media_retention_days: int | None = 30
        # Bounded per-account update buffer. Overflow drops the oldest entry and is
        # counted; consumers must resume from durable storage, not from this queue.
        self._updates: dict[str, deque[Update]] = defaultdict(deque)
        self._update_drops: dict[str, int] = defaultdict(int)
        self._closed = False

    async def close(self) -> None:
        """Idempotent shutdown: the adapter refuses service afterwards.

        Matches the boundary adapter contract, so a caller cannot keep using a
        closed adapter by accident (for example after a failed restart).
        """
        self._closed = True
        if self._store:
            self._store.close()

    @property
    def persistent(self) -> bool:
        return self._store is not None

    def _account(self, account_id: str) -> _Account:
        if account_id not in self._accounts:
            stored = self._store.load(account_id) if self._store else None
            account = _decode(stored) if stored else _fixture(account_id, self._clock())
            if self._store and stored is None:
                self._store.save(account_id, _encode(account))
            self._accounts[account_id] = account
        return self._accounts[account_id]

    def _commit(self, account_id: str, account: _Account) -> None:
        if self._store:
            self._store.save(account_id, _encode(account))
        self._accounts[account_id] = account

    def _expire_media(self, account: _Account) -> int:
        if self._media_retention_days is None:
            return 0
        cutoff = self._clock() - self._media_retention_days * 86400
        count = 0
        for messages in account.messages.values():
            for message in messages:
                media = message.get("media")
                if media and "data_base64" in media and media["created_at"] <= cutoff:
                    del media["data_base64"]
                    media["expired"] = True
                    count += 1
        photo = account.features.get("profile_photo")
        if photo and "data_base64" in photo and photo.get("created_at", 0) <= cutoff:
            del photo["data_base64"]
            photo["expired"] = True
            count += 1
        return count

    async def configure_media_retention(self, days: int | None) -> None:
        if days is not None and (type(days) is not int or days < 1):
            raise ValueError("invalid media retention")
        async with self._lock:
            self._media_retention_days = days

    async def prune_media(self, account_id: str) -> int:
        async with self._lock:
            if self._closed:
                raise SessionUnavailable(intervention_required=True)
            account = copy.deepcopy(self._account(account_id))
            count = self._expire_media(account)
            if count:
                self._commit(account_id, account)
            return count

    UPDATE_BUFFER_LIMIT = 1000
    UPDATE_TEXT_LIMIT = 4096

    def _emit(
        self,
        account_id: str,
        event_type: str,
        chat_id: str | None,
        payload: dict[str, Any],
        upstream_id: str,
    ) -> None:
        buffer = self._updates[account_id]
        if len(buffer) >= self.UPDATE_BUFFER_LIMIT:
            buffer.popleft()
            self._update_drops[account_id] += 1
        occurred = self._clock()
        buffer.append(
            Update(
                event_type=event_type,
                account_id=account_id,
                chat_id=chat_id,
                payload=payload,
                upstream_id=upstream_id,
                occurred_at=occurred,
            )
        )

    def _emit_message(
        self,
        account_id: str,
        event_type: str,
        chat_id: str,
        message_id: int,
        text: str | None,
        media: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"message_id": message_id}
        if text is not None:
            payload["text"] = text[: self.UPDATE_TEXT_LIMIT]
            payload["text_truncated"] = len(text) > self.UPDATE_TEXT_LIMIT
        if event_type == "message.new":
            payload["has_media"] = media is not None
            if media is not None:
                payload["media_kind"] = str(media.get("content_type"))[:127]
            payload["sender_ref"] = "self"
        self._emit(account_id, event_type, chat_id, payload, f"{chat_id}:{message_id}:{event_type}")

    def inject_update(self, update: Update) -> None:
        """Test-only fixture hook; never exposed through a client interface."""
        if update.account_id not in self._accounts and self._store is None:
            self._account(update.account_id)
        self._emit(
            update.account_id,
            update.event_type,
            update.chat_id,
            dict(update.payload),
            update.upstream_id or "fixture",
        )

    async def drain_updates(self, account_id: str, limit: int = 100) -> list[Update]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            # Normalized like every other adapter rejection: callers map
            # AdapterError to a typed API/MCP error, ValueError would be a 500.
            raise AdapterError("unsupported_operation")
        async with self._lock:
            if self._closed:
                raise SessionUnavailable(intervention_required=True)
            self._check(account_id, "drain_updates")
            buffer = self._updates[account_id]
            return [buffer.popleft() for _ in range(min(limit, len(buffer)))]

    def inject_fault(self, account_id: str, operation: str, error: Exception) -> None:
        """Test-only hook, never exposed through any client interface."""
        self._faults[(account_id, operation)].append(error)

    def set_state(self, account_id: str, state: str) -> None:
        if state not in {"ready", "disconnected", "reauth_required"}:
            raise ValueError("invalid synthetic session state")
        account = copy.deepcopy(self._account(account_id))
        account.state = state
        self._commit(account_id, account)

    async def status(self, account_id: str) -> SessionStatus:
        async with self._lock:
            if self._closed:
                raise SessionUnavailable(intervention_required=True)
            state = self._account(account_id).state
            return SessionStatus(state, state == "reauth_required")

    async def capability_profile(self, account_id: str) -> dict[str, bool]:
        """Return only synthetic eligibility flags, never credentials or content."""
        async with self._lock:
            if self._closed:
                raise SessionUnavailable(intervention_required=True)
            account = self._account(account_id)
            values = account.features.get("eligibility", {})
            return {
                key: value
                for key, value in values.items()
                if isinstance(key, str) and type(value) is bool
            }

    async def capability_chats(self, account_id: str) -> list[Chat]:
        """Return synthetic chat metadata for Core-side scope filtering only."""
        async with self._lock:
            if self._closed:
                raise SessionUnavailable(intervention_required=True)
            return list(self._account(account_id).chats.values())

    async def reconnect(self, account_id: str) -> SessionStatus:
        async with self._lock:
            if self._closed:
                raise SessionUnavailable(intervention_required=True)
            account = copy.deepcopy(self._account(account_id))
            if account.state == "reauth_required":
                raise SessionUnavailable(True)
            account.state = "ready"
            self._commit(account_id, account)
            return SessionStatus("ready")

    def _check(self, account_id: str, operation: str) -> _Account:
        if self._closed:
            raise SessionUnavailable(intervention_required=True)
        account = self._account(account_id)
        if account.state != "ready":
            raise SessionUnavailable(account.state == "reauth_required")
        failures = self._faults[(account_id, operation)]
        if failures:
            raise failures.popleft()
        return account

    async def list_chats(self, account_id: str) -> list[Chat]:
        async with self._lock:
            return list(self._check(account_id, "list_chats").chats.values())

    async def get_chat(self, account_id: str, chat_id: str) -> Chat | None:
        async with self._lock:
            return self._check(account_id, "get_chat").chats.get(chat_id)

    async def self_chat(self, account_id: str) -> Chat:
        async with self._lock:
            account = self._check(account_id, "self_chat")
            chat = account.chats.get("saved-messages")
            if chat is None:  # pragma: no cover - fixture invariant
                raise AdapterError("not_found")
            return chat

    async def execute(
        self, account_id: str, operation: str, chat_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            account = copy.deepcopy(self._check(account_id, operation))
            if operation in FEATURE_SPECS:
                return self._execute_feature(account_id, account, operation, chat_id, params)
            if chat_id not in account.chats:
                raise AdapterError("not_found")
            expired = self._expire_media(account)
            if expired:
                self._commit(account_id, account)
                # Keep subsequent mutations isolated until the next successful commit.
                account = copy.deepcopy(account)
            messages = account.messages[chat_id]

            def find(message_id: int) -> dict[str, Any]:
                match = next((m for m in messages if m["id"] == message_id), None)
                if match is None:
                    raise AdapterError("not_found")
                return match

            if operation in {"history", "search"}:
                items = messages
                if operation == "search":
                    query = params["query"].casefold()
                    items = [m for m in items if query in m["text"].casefold()]
                before_id = params.get("before_id")
                if before_id is not None:
                    items = [m for m in items if m["id"] < before_id]
                items = sorted(items, key=lambda m: m["id"], reverse=True)
                limit = params.get("limit", 50)
                page = items[:limit]
                return {
                    "messages": [_metadata(m) for m in page],
                    "next_before_id": page[-1]["id"] if len(items) > limit else None,
                    "sync_state": "synthetic",
                    "history_complete": False,
                }
            if operation == "get_message":
                return {"message": _metadata(find(params["message_id"]))}
            if operation == "get_media":
                media = find(params["message_id"])["media"]
                if media is None:
                    raise AdapterError("not_found")
                if "data_base64" not in media:
                    raise AdapterError("media_expired")
                result = copy.deepcopy(media)
                result.pop("created_at", None)
                return {"media": result}
            if operation in {"send", "send_media", "forward"}:
                if operation == "forward":
                    source = account.messages.get(params["source_chat_id"])
                    if source is None:
                        raise AdapterError("not_found")
                    original = next((m for m in source if m["id"] == params["message_id"]), None)
                    if original is None:
                        raise AdapterError("not_found")
                    text, media = original["text"], copy.deepcopy(original["media"])
                    if media is not None and "data_base64" not in media:
                        raise AdapterError("media_expired")
                else:
                    text = params.get("text", "")
                    media = copy.deepcopy(params.get("media"))
                    if media is not None:
                        media["created_at"] = self._clock()
                        media["size"] = len(base64.b64decode(media["data_base64"]))
                if operation == "send" and params.get("reply_to_message_id") is not None:
                    reply_id = params["reply_to_message_id"]
                    if not any(message["id"] == reply_id for message in messages):
                        raise AdapterError("not_found")
                if (
                    operation == "send"
                    and params.get("message_effect_id")
                    and not account.features.get("eligibility", {}).get("premium", False)
                ):
                    raise AdapterError("capability_unavailable")
                new_id = account.next_id
                account.next_id += 1
                message = {"id": new_id, "chat_id": chat_id, "text": text, "media": media}
                if operation == "send":
                    for field in ("reply_to_message_id", "quote_text", "message_effect_id"):
                        if params.get(field) is not None:
                            message[field] = params[field]
                messages.append(message)
                self._commit(account_id, account)
                self._emit_message(account_id, "message.new", chat_id, new_id, text, media)
                return {"message_id": new_id}
            if operation == "edit":
                find(params["message_id"])["text"] = params["text"]
                self._commit(account_id, account)
                self._emit_message(
                    account_id, "message.edited", chat_id, params["message_id"], params["text"]
                )
                return {"message_id": params["message_id"]}
            if operation == "delete":
                messages.remove(find(params["message_id"]))
                self._commit(account_id, account)
                self._emit_message(
                    account_id, "message.deleted", chat_id, params["message_id"], None
                )
                return {"message_id": params["message_id"]}
            if operation == "set_title":
                old = account.chats[chat_id]
                account.chats[chat_id] = Chat(old.id, old.kind, params["title"])
                self._commit(account_id, account)
                self._emit(
                    account_id,
                    "chat.updated",
                    chat_id,
                    {"title": params["title"][:256]},
                    f"{chat_id}:title:"
                    + hashlib.sha256(params["title"].encode("utf-8")).hexdigest()[:32],
                )
                return {"updated": True}
            if operation == "set_member_status":
                account.members.setdefault(chat_id, {})[params["member_id"]] = params["status"]
                self._commit(account_id, account)
                return {"updated": True}
            raise AdapterError("unsupported_operation")

    def _execute_feature(
        self,
        account_id: str,
        account: _Account,
        operation: str,
        chat_id: str,
        outer_params: dict[str, Any],
    ) -> dict[str, Any]:
        """Execute one normalized feature action against synthetic account state."""
        spec = FEATURE_SPECS.get(operation)
        if spec is None:
            raise AdapterError("unsupported_operation")
        params = outer_params.get("params", {})
        if not isinstance(params, dict):
            raise AdapterError("unsupported_operation")
        if spec.chat_required and chat_id not in account.chats:
            raise AdapterError("not_found")
        if operation == "translate_message":
            # Translation may transmit Telegram-derived content to a provider. It
            # remains closed regardless of synthetic fixture status until the
            # compliance decision and a task-specific owner approval are recorded.
            raise AdapterError("compliance_blocked")
        if spec.blocked_reason:
            raise AdapterError("approval_required")
        eligibility = account.features.setdefault("eligibility", {})
        if spec.platform_dependency and not eligibility.get(spec.platform_dependency, False):
            raise AdapterError("capability_unavailable")
        if operation not in SYNTHETIC_FEATURE_OPERATIONS:
            raise AdapterError(
                "unsupported_platform" if spec.platform_dependency else "unsupported_operation"
            )

        state = account.features
        target_chat = chat_id if spec.chat_required else None
        admin_rights = {
            "invite_user": "invite_users",
            "list_invite_links": "manage_invite_links",
            "create_invite_link": "manage_invite_links",
            "revoke_invite_link": "manage_invite_links",
            "ban_member": "ban_users",
            "unban_member": "ban_users",
            "set_member_role": "promote_members",
            "set_chat_permissions": "manage_permissions",
            "set_slow_mode": "manage_slow_mode",
            "get_admin_log": "view_admin_log",
            "create_topic": "manage_topics",
            "edit_topic": "manage_topics",
            "delete_topic": "manage_topics",
            "pin_message": "pin_messages",
            "unpin_message": "pin_messages",
            "set_allowed_reactions": "manage_reactions",
            "update_channel_settings": "change_info",
            "list_join_requests": "manage_join_requests",
            "approve_join_request": "manage_join_requests",
            "decline_join_request": "manage_join_requests",
            "set_auto_delete_timer": "change_info",
        }
        required_right = admin_rights.get(operation)
        if required_right and required_right not in state.get("chat_rights", {}).get(chat_id, []):
            raise AdapterError("permission_required")
        if operation == "create_group" and "create_group" not in state.get("account_rights", []):
            raise AdapterError("permission_required")
        if operation == "terminate_session" and "manage_sessions" not in state.get(
            "account_rights", []
        ):
            raise AdapterError("permission_required")

        def message(message_id: int, *, in_chat: str | None = None) -> dict[str, Any]:
            selected_chat = in_chat or chat_id
            rows = account.messages.get(selected_chat)
            if rows is None:
                raise AdapterError("not_found")
            found = next((row for row in rows if row["id"] == message_id), None)
            if found is None:
                raise AdapterError("not_found")
            return found

        def story_by_id(
            story_id: str, *, in_chat: str | None = None, owner_only: bool = False
        ) -> dict[str, Any]:
            story = next(
                (row for row in state.setdefault("stories", []) if row["story_id"] == story_id),
                None,
            )
            if story is None or (in_chat is not None and story.get("chat_id") != in_chat):
                raise AdapterError("not_found")
            if owner_only and story.get("owner_ref") != "self":
                raise AdapterError("permission_required")
            return story

        def add_admin_log(event: str, target: str | None = None) -> None:
            entries = state.setdefault("admin_log", [])
            row = {"event": event, "target_ref": target, "timestamp": self._clock()}
            entries.append(row)
            del entries[:-500]

        def emit(
            event: str, payload: dict[str, Any], aggregate_type: str, aggregate_id: str
        ) -> None:
            self._emit(
                account_id,
                event,
                target_chat,
                payload,
                f"{aggregate_type}:{aggregate_id}:{uuid.uuid4().hex}",
            )

        def commit() -> None:
            self._commit(account_id, account)

        if operation == "get_chat_info":
            chat = account.chats[chat_id]
            return {
                "chat": asdict(chat),
                "member_count": len(account.members.get(chat_id, {})),
                "unread": self._unread_summary(account, chat_id),
                "state": copy.deepcopy(state.get("chat_settings", {}).get(chat_id, {})),
                "capabilities": {
                    "forum": bool(state.get("topics", {}).get(chat_id)),
                    "admin_rights": list(state.get("chat_rights", {}).get(chat_id, [])),
                },
            }
        if operation == "get_replies":
            rows = [
                _metadata(row)
                for row in account.messages[chat_id]
                if row.get("reply_to_message_id") == params["message_id"]
            ]
            limit = params.get("limit", 50)
            return {"messages": rows[:limit], "has_more": len(rows) > limit, "complete": False}
        if operation == "get_permalink":
            row = message(params["message_id"])
            chat = account.chats[chat_id]
            if chat.kind not in {ChatKind.PUBLIC_CHANNEL, ChatKind.PUBLIC_GROUP}:
                return {"available": False, "permalink": None, "reason": "private_chat"}
            return {
                "available": True,
                "permalink": f"synthetic://telegram/{chat_id}/{row['id']}",
                "is_telegram_url": False,
            }
        if operation == "get_unread":
            return self._unread_summary(account, chat_id)
        if operation == "mark_read":
            rows = account.messages[chat_id]
            newest = max((row["id"] for row in rows), default=0)
            read_to = min(params["up_to_id"], newest)
            state.setdefault("read_up_to", {})[chat_id] = max(
                read_to, state.setdefault("read_up_to", {}).get(chat_id, 0)
            )
            commit()
            emit("message.read", {"up_to_id": read_to, "message_id": read_to}, "chat", chat_id)
            return {"read_up_to": read_to}
        if operation == "send_typing":
            until = self._clock() + 5
            state.setdefault("typing", {})[chat_id] = until
            commit()
            return {"active": True, "expires_at": until, "bounded_seconds": 5}
        if operation == "get_draft":
            return {"draft": copy.deepcopy(state.setdefault("drafts", {}).get(chat_id))}
        if operation == "set_draft":
            draft = {
                "text": params["text"],
                "reply_to_message_id": params.get("reply_to_message_id"),
                "updated_at": self._clock(),
            }
            if draft["reply_to_message_id"] is not None:
                message(draft["reply_to_message_id"])
            state.setdefault("drafts", {})[chat_id] = draft
            commit()
            return {"updated": True, "draft": copy.deepcopy(draft)}
        if operation == "clear_draft":
            state.setdefault("drafts", {}).pop(chat_id, None)
            commit()
            return {"cleared": True}
        if operation == "list_scheduled_messages":
            scheduled = state.setdefault("scheduled_messages", {}).get(chat_id, [])
            return {"messages": copy.deepcopy(scheduled), "complete": False}
        if operation == "schedule_message":
            run_at = params["run_at"]
            if isinstance(run_at, datetime):
                run_at = run_at.isoformat()
            item = {
                "scheduled_id": f"scheduled-{uuid.uuid4().hex[:16]}",
                "text": params["text"],
                "run_at": run_at,
                "state": "scheduled",
            }
            state.setdefault("scheduled_messages", {}).setdefault(chat_id, []).append(item)
            commit()
            return copy.deepcopy(item)
        if operation == "delete_scheduled_message":
            rows = state.setdefault("scheduled_messages", {}).get(chat_id, [])
            before = len(rows)
            rows[:] = [row for row in rows if row["scheduled_id"] != params["scheduled_id"]]
            if len(rows) == before:
                raise AdapterError("not_found")
            commit()
            return {"deleted": True, "scheduled_id": params["scheduled_id"]}
        if operation == "send_album":
            items = params["items"]
            staged = [self._validated_feature_media(item) for item in items]
            if any(not item["content_type"].startswith(("image/", "video/")) for item in staged):
                raise AdapterError("unsupported_operation")
            album_id = f"album-{uuid.uuid4().hex[:16]}"
            results = []
            for item in staged:
                message_id = account.next_id
                account.next_id += 1
                row = {
                    "id": message_id,
                    "chat_id": chat_id,
                    "text": params.get("caption", ""),
                    "media": {**item, "created_at": self._clock(), "album_id": album_id},
                    "album_id": album_id,
                }
                account.messages[chat_id].append(row)
                results.append({"index": len(results), "message_id": message_id})
            commit()
            for item in results:
                self._emit_message(
                    account_id,
                    "message.new",
                    chat_id,
                    item["message_id"],
                    params.get("caption", ""),
                    None,
                )
            return {"album_id": album_id, "items": results, "atomic": False}
        if operation in {"send_voice", "send_video_note"}:
            media = self._validated_feature_media(params)
            if operation == "send_voice" and media["content_type"] not in {
                "audio/ogg",
                "audio/mpeg",
                "audio/mp4",
                "audio/webm",
            }:
                raise AdapterError("unsupported_operation")
            if operation == "send_video_note" and media["content_type"] not in {
                "video/mp4",
                "video/webm",
            }:
                raise AdapterError("unsupported_operation")
            message_id = account.next_id
            account.next_id += 1
            kind = "voice" if operation == "send_voice" else "video_note"
            account.messages[chat_id].append(
                {
                    "id": message_id,
                    "chat_id": chat_id,
                    "text": f"[synthetic {kind}]",
                    "media": {**media, "created_at": self._clock()},
                    "media_kind": kind,
                }
            )
            commit()
            self._emit_message(
                account_id,
                "message.new",
                chat_id,
                message_id,
                None,
                {"content_type": kind},
            )
            return {"message_id": message_id, "media_kind": kind}
        if operation == "send_rich_message":
            rich = dict(params)
            if rich.get("kind") == "contact":
                phone = rich.get("phone", "")
                if not (phone.startswith("+120255501") and len(phone) == 12):
                    raise AdapterError("unsupported_operation")
            message_id = account.next_id
            account.next_id += 1
            text = f"[synthetic {rich['kind']} message]"
            account.messages[chat_id].append(
                {
                    "id": message_id,
                    "chat_id": chat_id,
                    "text": text,
                    "media": None,
                    "rich_content": rich,
                }
            )
            commit()
            self._emit_message(account_id, "message.new", chat_id, message_id, text, None)
            return {"message_id": message_id, "kind": rich["kind"]}
        if operation == "get_reactions":
            message(params["message_id"])
            key = f"{chat_id}:{params['message_id']}"
            values = copy.deepcopy(state.setdefault("reactions", {}).get(key, []))
            return {"message_id": params["message_id"], "reactions": values, "complete": False}
        if operation == "set_reaction":
            message(params["message_id"])
            if params.get("paid"):
                raise AdapterError("approval_required")
            key = f"{chat_id}:{params['message_id']}"
            values = state.setdefault("reactions", {}).setdefault(key, [])
            values[:] = [row for row in values if row.get("user_ref") != "self"]
            if not params.get("remove"):
                values.append(
                    {
                        "reaction": params["reaction"],
                        "user_ref": "self",
                        "paid": params.get("paid", False),
                    }
                )
            commit()
            emit(
                "reaction.updated",
                {
                    "message_id": params["message_id"],
                    "reaction": params["reaction"],
                    "count": len(values),
                },
                "message",
                str(params["message_id"]),
            )
            return {"updated": True, "removed": params.get("remove", False), "count": len(values)}
        if operation == "get_default_reactions":
            return copy.deepcopy(
                state.get("default_reaction", {"reaction": None, "recent": [], "top": []})
            )
        if operation == "set_default_reaction":
            state["default_reaction"] = {
                "reaction": params["reaction"],
                "recent": [params["reaction"]],
                "top": [params["reaction"]],
            }
            commit()
            return {"updated": True, **copy.deepcopy(state["default_reaction"])}
        if operation == "create_poll":
            message_id = account.next_id
            account.next_id += 1
            poll = {
                "message_id": message_id,
                "question": params["question"],
                "options": [{"text": option, "voters": 0} for option in params["options"]],
                "multiple_answers": params.get("multiple_answers", False),
                "anonymous": params.get("anonymous", True),
                "closed": False,
                "voters": [],
                "creator_ref": "self",
            }
            state.setdefault("polls", {})[f"{chat_id}:{message_id}"] = poll
            account.messages[chat_id].append(
                {
                    "id": message_id,
                    "chat_id": chat_id,
                    "text": poll["question"],
                    "media": None,
                    "poll": True,
                }
            )
            commit()
            emit(
                "poll.updated",
                {"message_id": message_id, "options_count": len(poll["options"]), "closed": False},
                "message",
                str(message_id),
            )
            return {"message_id": message_id, "poll": copy.deepcopy(poll)}
        if operation == "get_poll":
            message(params["message_id"])
            poll = state.setdefault("polls", {}).get(f"{chat_id}:{params['message_id']}")
            if poll is None:
                raise AdapterError("not_found")
            result = copy.deepcopy(poll)
            if result["anonymous"]:
                result.pop("voters", None)
            return {"poll": result, "complete": False}
        if operation == "vote_poll":
            poll = state.setdefault("polls", {}).get(f"{chat_id}:{params['message_id']}")
            if poll is None:
                raise AdapterError("not_found")
            if poll["closed"]:
                raise AdapterError("poll_closed")
            indexes = params["option_indexes"]
            if any(index >= len(poll["options"]) for index in indexes):
                raise AdapterError("not_found")
            if len(indexes) > 1 and not poll["multiple_answers"]:
                raise AdapterError("unsupported_operation")
            voter = next((row for row in poll["voters"] if row["user_ref"] == "self"), None)
            previous = set(voter["option_indexes"]) if voter else set()
            for index in previous:
                poll["options"][index]["voters"] -= 1
            for index in indexes:
                poll["options"][index]["voters"] += 1
            poll["voters"] = [row for row in poll["voters"] if row["user_ref"] != "self"]
            if not poll["anonymous"]:
                poll["voters"].append({"user_ref": "self", "option_indexes": list(indexes)})
            commit()
            emit(
                "poll.updated",
                {
                    "message_id": params["message_id"],
                    "options_count": len(poll["options"]),
                    "closed": False,
                },
                "message",
                str(params["message_id"]),
            )
            result = copy.deepcopy(poll)
            if result["anonymous"]:
                result.pop("voters", None)
            return {"poll": result, "voted": True}
        if operation == "close_poll":
            poll = state.setdefault("polls", {}).get(f"{chat_id}:{params['message_id']}")
            if poll is None:
                raise AdapterError("not_found")
            if poll["creator_ref"] != "self":
                raise AdapterError("permission_required")
            poll["closed"] = True
            commit()
            emit(
                "poll.updated",
                {
                    "message_id": params["message_id"],
                    "options_count": len(poll["options"]),
                    "closed": True,
                },
                "message",
                str(params["message_id"]),
            )
            return {"closed": True}
        if operation == "list_sticker_sets":
            query = params.get("query", "").casefold()
            sets = [
                row for row in state.get("sticker_sets", []) if query in row["title"].casefold()
            ]
            return {"sets": copy.deepcopy(sets)}
        if operation == "send_sticker":
            sticker_set = next(
                (row for row in state.get("sticker_sets", []) if row["set_id"] == params["set_id"]),
                None,
            )
            if sticker_set is None or params["sticker_id"] not in sticker_set["stickers"]:
                raise AdapterError("not_found")
            message_id = account.next_id
            account.next_id += 1
            account.messages[chat_id].append(
                {
                    "id": message_id,
                    "chat_id": chat_id,
                    "text": params.get("emoji") or "[sticker]",
                    "media": None,
                    "sticker": {"set_id": params["set_id"], "sticker_id": params["sticker_id"]},
                }
            )
            commit()
            self._emit_message(
                account_id,
                "message.new",
                chat_id,
                message_id,
                "[sticker]",
                {"content_type": "sticker"},
            )
            return {"message_id": message_id}
        if operation in {"get_buttons", "get_web_app"}:
            row = message(params["message_id"])
            buttons = copy.deepcopy(row.get("buttons", []))
            if operation == "get_buttons":
                return {"message_id": params["message_id"], "buttons": buttons}
            button_id = params.get("button_id")
            button = next(
                (
                    item
                    for item in buttons
                    if item.get("button_id") == button_id and item.get("kind") == "web_app"
                ),
                None,
            )
            if button is None:
                raise AdapterError("not_found")
            return {
                "button_id": button["button_id"],
                "kind": "web_app",
                "domain": button.get("domain"),
                "executable": False,
            }
        if operation == "press_button":
            row = message(params["message_id"])
            button = next(
                (
                    item
                    for item in row.get("buttons", [])
                    if item.get("button_id") == params.get("button_id")
                ),
                None,
            )
            if button is None:
                raise AdapterError("not_found")
            if button.get("kind") != "callback":
                raise AdapterError("capability_unavailable")
            return {
                "pressed": True,
                "button_id": button["button_id"],
                "result": "synthetic_callback_ack",
            }
        if operation == "get_members":
            rows = account.members.get(chat_id, {})
            limit = params.get("limit", 100)
            members = [{"user_id": user_id, "role": role} for user_id, role in sorted(rows.items())]
            return {"members": members[:limit], "has_more": len(members) > limit, "complete": False}
        if operation in {"join_chat", "leave_chat"}:
            joined = state.setdefault("joined", [])
            if operation == "join_chat":
                if account.chats[chat_id].kind in {ChatKind.PRIVATE_GROUP, ChatKind.PRIVATE_DIRECT}:
                    raise AdapterError("permission_required")
                if chat_id not in joined:
                    joined.append(chat_id)
            else:
                joined[:] = [item for item in joined if item != chat_id]
            commit()
            return {"joined": operation == "join_chat", "chat_id": chat_id}
        if operation == "create_group":
            title = params["title"]
            new_chat_id = f"synthetic-group-{account.next_id}"
            account.next_id += 1
            chat = Chat(new_chat_id, ChatKind.PRIVATE_GROUP, title)
            account.chats[new_chat_id] = chat
            account.messages[new_chat_id] = []
            account.members[new_chat_id] = {
                member_id: "member" for member_id in params.get("member_ids", [])
            }
            state.setdefault("topics", {})[new_chat_id] = []
            state.setdefault("chat_rights", {})[new_chat_id] = list(
                state.get("chat_rights", {}).get("private-group", [])
            )
            state.setdefault("joined", []).append(new_chat_id)
            commit()
            return {"chat": asdict(chat), "member_count": len(account.members[new_chat_id])}
        if operation == "invite_user":
            user_id = params["user_id"]
            if user_id in state.setdefault("blocked_users", []):
                raise AdapterError("account_restricted")
            account.members.setdefault(chat_id, {})[user_id] = "member"
            add_admin_log(operation, user_id)
            commit()
            emit(
                "member.updated",
                {"user_id": user_id, "role": "member", "status": "invited"},
                "chat",
                chat_id,
            )
            return {"invited": True, "user_id": user_id}
        if operation == "list_invite_links":
            return {
                "invite_links": copy.deepcopy(state.setdefault("invite_links", {}).get(chat_id, []))
            }
        if operation == "create_invite_link":
            row = {
                "invite_id": f"invite-{uuid.uuid4().hex[:16]}",
                "name": params.get("name", ""),
                "expire_at": params.get("expire_at").isoformat()
                if isinstance(params.get("expire_at"), datetime)
                else None,
                "member_limit": params.get("member_limit"),
                "revoked": False,
            }
            state.setdefault("invite_links", {}).setdefault(chat_id, []).append(row)
            add_admin_log(operation, row["invite_id"])
            commit()
            return copy.deepcopy(row)
        if operation == "revoke_invite_link":
            rows = state.setdefault("invite_links", {}).get(chat_id, [])
            row = next((item for item in rows if item["invite_id"] == params["invite_id"]), None)
            if row is None:
                raise AdapterError("not_found")
            row["revoked"] = True
            add_admin_log(operation, row["invite_id"])
            commit()
            return {"revoked": True, "invite_id": row["invite_id"]}
        if operation in {"ban_member", "unban_member", "set_member_role"}:
            user_id = params["user_id"]
            role = (
                "banned"
                if operation == "ban_member"
                else "member"
                if operation == "unban_member"
                else params["role"]
            )
            account.members.setdefault(chat_id, {})[user_id] = role
            add_admin_log(operation, user_id)
            commit()
            emit(
                "member.updated",
                {"user_id": user_id, "role": role, "status": "updated"},
                "chat",
                chat_id,
            )
            return {"updated": True, "user_id": user_id, "role": role}
        if operation == "set_chat_permissions":
            current = state.setdefault("chat_permissions", {}).setdefault(chat_id, {})
            current.update({key: value for key, value in params.items() if value is not None})
            add_admin_log(operation)
            commit()
            return {"permissions": copy.deepcopy(current)}
        if operation == "set_slow_mode":
            state.setdefault("chat_settings", {}).setdefault(chat_id, {})["slow_mode_seconds"] = (
                params["seconds"]
            )
            add_admin_log(operation)
            commit()
            emit(
                "chat.state_changed",
                {"state": "slow_mode_seconds", "value": params["seconds"]},
                "chat",
                chat_id,
            )
            return {"slow_mode_seconds": params["seconds"]}
        if operation == "get_admin_log":
            limit = params.get("limit", 100)
            return {
                "entries": copy.deepcopy(state.get("admin_log", [])[-limit:]),
                "complete": False,
            }
        if operation == "list_topics":
            return {
                "topics": copy.deepcopy(state.setdefault("topics", {}).get(chat_id, [])),
                "complete": False,
            }
        if operation == "get_topic_unread":
            topic = next(
                (
                    row
                    for row in state.setdefault("topics", {}).get(chat_id, [])
                    if row["topic_id"] == params["topic_id"]
                ),
                None,
            )
            if topic is None:
                raise AdapterError("not_found")
            return {"topic_id": topic["topic_id"], "unread_count": topic.get("unread_count", 0)}
        if operation == "create_topic":
            topics = state.setdefault("topics", {}).setdefault(chat_id, [])
            if len(topics) >= 1000:
                raise AdapterError("limit_exceeded")
            topic_id = f"topic-{uuid.uuid4().hex[:12]}"
            topic = {
                "topic_id": topic_id,
                "title": params["title"],
                "icon_emoji_id": params.get("icon_emoji_id"),
                "unread_count": 0,
                "closed": False,
            }
            topics.append(topic)
            add_admin_log(operation, topic_id)
            commit()
            emit(
                "topic.updated",
                {"topic_id": topic_id, "title": topic["title"], "state": "created"},
                "topic",
                topic_id,
            )
            return copy.deepcopy(topic)
        if operation in {"edit_topic", "delete_topic"}:
            topics = state.setdefault("topics", {}).setdefault(chat_id, [])
            topic_id = params["topic_id"]
            topic = next((row for row in topics if row["topic_id"] == topic_id), None)
            if topic is None:
                raise AdapterError("not_found")
            if operation == "delete_topic":
                topics.remove(topic)
                result = {"deleted": True, "topic_id": topic_id}
                event_state = "deleted"
            else:
                topic["title"] = params["title"]
                result = copy.deepcopy(topic)
                event_state = "updated"
            add_admin_log(operation, topic_id)
            commit()
            emit(
                "topic.updated",
                {"topic_id": topic_id, "title": topic.get("title", ""), "state": event_state},
                "topic",
                topic_id,
            )
            return result
        if operation in {"pin_message", "unpin_message"}:
            row = message(params["message_id"])
            pins = state.setdefault("pinned_messages", {}).setdefault(chat_id, [])
            if operation == "pin_message":
                if row["id"] not in pins:
                    pins.append(row["id"])
            else:
                pins[:] = [message_id for message_id in pins if message_id != row["id"]]
            add_admin_log(operation, str(row["id"]))
            commit()
            return {"pinned": operation == "pin_message", "message_id": row["id"]}
        if operation == "get_chat_state":
            result = copy.deepcopy(state.setdefault("chat_settings", {}).get(chat_id, {}))
            result["joined"] = chat_id in state.setdefault("joined", [])
            result["pinned_messages"] = list(
                state.setdefault("pinned_messages", {}).get(chat_id, [])
            )
            return result
        if operation == "set_chat_state":
            field = params["field"]
            value = params["value"]
            if field == "unread":
                if value:
                    state.setdefault("read_up_to", {})[chat_id] = 0
                else:
                    state.setdefault("read_up_to", {})[chat_id] = max(
                        (row["id"] for row in account.messages[chat_id]), default=0
                    )
            else:
                state.setdefault("chat_settings", {}).setdefault(chat_id, {})[field] = value
            commit()
            emit("chat.state_changed", {"state": field, "value": value}, "chat", chat_id)
            return {field: value}
        if operation == "list_chat_folders":
            return {"folders": copy.deepcopy(state.setdefault("chat_folders", []))}
        if operation == "save_chat_folder":
            folders = state.setdefault("chat_folders", [])
            folder_id = params.get("folder_id")
            folder = (
                next((row for row in folders if row["folder_id"] == folder_id), None)
                if folder_id
                else None
            )
            if folder_id and folder is None:
                raise AdapterError("not_found")
            if folder is None:
                folder = {"folder_id": f"folder-{uuid.uuid4().hex[:12]}", "created_by": "client"}
                folders.append(folder)
            for key in (
                "title",
                "chat_ids",
                "excluded_chat_ids",
                "include_muted",
                "include_read",
                "include_archived",
            ):
                value = params.get(key)
                if value is not None:
                    folder[key] = copy.deepcopy(value)
            commit()
            return {"folder": copy.deepcopy(folder)}
        if operation == "delete_chat_folder":
            folders = state.setdefault("chat_folders", [])
            folder = next((row for row in folders if row["folder_id"] == params["folder_id"]), None)
            if folder is None:
                raise AdapterError("not_found")
            folders.remove(folder)
            commit()
            return {"deleted": True, "folder_id": params["folder_id"]}
        if operation == "reorder_chat_folders":
            folders = state.setdefault("chat_folders", [])
            by_id = {row["folder_id"]: row for row in folders}
            if set(params["folder_ids"]) != set(by_id):
                raise AdapterError("not_found")
            state["chat_folders"] = [by_id[folder_id] for folder_id in params["folder_ids"]]
            commit()
            return {"folder_ids": list(params["folder_ids"])}
        if operation == "get_own_role":
            rights = state.get("chat_rights", {}).get(chat_id, [])
            return {
                "role": "administrator" if rights else "member",
                "admin_rights": list(rights),
                "synthetic": True,
            }
        if operation == "get_channel_settings":
            return copy.deepcopy(
                state.setdefault("channel_settings", {}).get(
                    chat_id,
                    {
                        "signatures_enabled": False,
                        "anti_spam_enabled": False,
                        "forum_enabled": bool(state.get("topics", {}).get(chat_id)),
                        "prehistory_enabled": True,
                    },
                )
            )
        if operation == "update_channel_settings":
            settings = state.setdefault("channel_settings", {}).setdefault(
                chat_id,
                {
                    "signatures_enabled": False,
                    "anti_spam_enabled": False,
                    "forum_enabled": bool(state.get("topics", {}).get(chat_id)),
                    "prehistory_enabled": True,
                },
            )
            settings.update({key: value for key, value in params.items() if value is not None})
            add_admin_log(operation)
            commit()
            return {"updated": True, "settings": copy.deepcopy(settings)}
        if operation == "list_join_requests":
            rows = state.setdefault("join_requests", {}).get(chat_id, [])
            limit = params.get("limit", 100)
            return {"requests": copy.deepcopy(rows[:limit]), "has_more": len(rows) > limit}
        if operation in {"approve_join_request", "decline_join_request"}:
            requests = state.setdefault("join_requests", {}).setdefault(chat_id, [])
            user_id = params["user_id"]
            request = next((row for row in requests if row["user_id"] == user_id), None)
            if request is None:
                raise AdapterError("not_found")
            requests.remove(request)
            if operation == "approve_join_request":
                account.members.setdefault(chat_id, {})[user_id] = "member"
            add_admin_log(operation, user_id)
            commit()
            return {"approved": operation == "approve_join_request", "user_id": user_id}
        if operation == "get_auto_delete_timer":
            return {"seconds": state.setdefault("auto_delete_seconds", {}).get(chat_id, 0)}
        if operation == "set_auto_delete_timer":
            seconds = params["seconds"]
            state.setdefault("auto_delete_seconds", {})[chat_id] = seconds
            add_admin_log(operation)
            commit()
            emit(
                "chat.state_changed",
                {"state": "auto_delete_seconds", "value": seconds},
                "chat",
                chat_id,
            )
            return {"seconds": seconds}
        if operation == "set_allowed_reactions":
            reactions = list(dict.fromkeys(params["reactions"]))
            state.setdefault("allowed_reactions", {})[chat_id] = reactions
            add_admin_log(operation)
            commit()
            return {"reactions": reactions}
        if operation == "list_contacts":
            return {"contacts": copy.deepcopy(state.setdefault("contacts", []))}
        if operation == "add_contact":
            user_id = params["user_id"]
            contacts = state.setdefault("contacts", [])
            existing = next((row for row in contacts if row["user_id"] == user_id), None)
            if existing is None:
                contacts.append(
                    {
                        "user_id": user_id,
                        "first_name": params.get("first_name") or "Synthetic",
                        "last_name": params.get("last_name") or "",
                    }
                )
            commit()
            return {"added": True, "user_id": user_id}
        if operation == "remove_contact":
            user_id = params["user_id"]
            contacts = state.setdefault("contacts", [])
            before = len(contacts)
            contacts[:] = [row for row in contacts if row["user_id"] != user_id]
            if len(contacts) == before:
                raise AdapterError("not_found")
            commit()
            return {"removed": True, "user_id": user_id}
        if operation == "list_contact_requests":
            return {"requests": copy.deepcopy(state.setdefault("contact_requests", []))}
        if operation == "accept_contact_request":
            user_id = params["user_id"]
            requests = state.setdefault("contact_requests", [])
            request = next((row for row in requests if row["user_id"] == user_id), None)
            if request is None:
                raise AdapterError("not_found")
            requests.remove(request)
            state.setdefault("contacts", []).append(copy.deepcopy(request))
            commit()
            return {"accepted": True, "user_id": user_id}
        if operation in {"block_user", "unblock_user"}:
            blocked = state.setdefault("blocked_users", [])
            user_id = params["user_id"]
            if operation == "block_user" and user_id not in blocked:
                blocked.append(user_id)
            if operation == "unblock_user":
                blocked[:] = [row for row in blocked if row != user_id]
            commit()
            return {"blocked": operation == "block_user", "user_id": user_id}
        if operation == "report_user":
            report = {
                "target_ref": params["user_id"],
                "chat_ref": chat_id,
                "reason": params["reason"],
                "details_recorded": False,
            }
            state.setdefault("reports", []).append(report)
            add_admin_log(operation, params["user_id"])
            commit()
            return {"recorded": True, "target_ref": params["user_id"], "synthetic": True}
        if operation == "get_profile":
            return {"profile": copy.deepcopy(state.get("profile", {}))}
        if operation == "update_profile":
            profile = state.setdefault("profile", {})
            profile.update({key: value for key, value in params.items() if value is not None})
            commit()
            return {"updated": True, "profile": copy.deepcopy(profile)}
        if operation == "update_profile_details":
            profile = state.setdefault("profile", {})
            updates = {key: value for key, value in params.items() if value is not None}
            if isinstance(updates.get("birthday"), date):
                updates["birthday"] = updates["birthday"].isoformat()
            profile.update(updates)
            commit()
            return {"updated": True, "profile": copy.deepcopy(profile)}
        if operation == "get_reaction_privacy":
            return copy.deepcopy(state.setdefault("reaction_privacy", {"show_reactions": True}))
        if operation == "update_reaction_privacy":
            state["reaction_privacy"] = {"show_reactions": params["show_reactions"]}
            commit()
            return {"updated": True, **copy.deepcopy(state["reaction_privacy"])}
        if operation == "get_privacy":
            return {"settings": copy.deepcopy(state.get("privacy", {}))}
        if operation == "update_privacy":
            privacy = state.setdefault("privacy", {})
            privacy.update(params["settings"])
            commit()
            return {"updated": True, "settings": copy.deepcopy(privacy)}
        if operation == "list_sessions":
            # Only safe metadata is exposed; no auth key, session material or code.
            return {
                "sessions": [
                    {
                        key: value
                        for key, value in row.items()
                        if key in {"session_id", "current", "device", "state"}
                    }
                    for row in state.setdefault("sessions", [])
                ]
            }
        if operation == "terminate_session":
            session = next(
                (
                    row
                    for row in state.setdefault("sessions", [])
                    if row["session_id"] == params["session_id"]
                ),
                None,
            )
            if session is None or session.get("current"):
                raise AdapterError("unsupported_operation")
            session["state"] = "terminated"
            commit()
            return {"terminated": True, "session_id": session["session_id"]}
        if operation in {
            "list_stories",
            "get_story",
            "create_story",
            "update_story",
            "delete_story",
            "get_story_stats",
        }:
            stories = state.setdefault("stories", [])
            if operation == "list_stories":
                rows = [
                    row
                    for row in stories
                    if target_chat is None or row.get("chat_id") == target_chat
                ]
                return {
                    "stories": copy.deepcopy(rows[: params.get("limit", 50)]),
                    "complete": False,
                }
            story_id = params.get("story_id")
            story = (
                next((row for row in stories if row["story_id"] == story_id), None)
                if story_id
                else None
            )
            if story is not None and spec.chat_required and story.get("chat_id") != chat_id:
                story = None
            if operation == "create_story":
                story = {
                    "story_id": f"story-{uuid.uuid4().hex[:16]}",
                    "chat_id": "self",
                    "owner_ref": "self",
                    "content": params["content"],
                    "privacy": params["privacy"],
                    "state": "active",
                    "views": 0,
                    "reactions": 0,
                    "expires_at": self._clock() + params["expires_in_hours"] * 3600,
                }
                stories.append(story)
                commit()
                self._emit(
                    account_id,
                    "story.updated",
                    None,
                    {"story_id": story["story_id"], "state": "created"},
                    f"story:{story['story_id']}:created",
                )
                return {"story": copy.deepcopy(story)}
            if story is None:
                raise AdapterError("not_found")
            if operation == "get_story":
                return {"story": copy.deepcopy(story)}
            if operation == "get_story_stats":
                if story.get("owner_ref") != "self":
                    raise AdapterError("permission_required")
                return {
                    "story_id": story["story_id"],
                    "views": story.get("views", 0),
                    "reactions": story.get("reactions", 0),
                }
            if operation in {"update_story", "delete_story"} and story.get("owner_ref") != "self":
                raise AdapterError("permission_required")
            if operation == "update_story":
                if params.get("content") is None:
                    raise AdapterError("unsupported_operation")
                story["content"] = params["content"]
                commit()
                self._emit(
                    account_id,
                    "story.updated",
                    None,
                    {"story_id": story["story_id"], "state": "updated"},
                    f"story:{story['story_id']}:updated",
                )
                return {"story": copy.deepcopy(story)}
            if operation == "delete_story":
                stories.remove(story)
                commit()
                self._emit(
                    account_id,
                    "story.updated",
                    None,
                    {"story_id": story_id, "state": "deleted"},
                    f"story:{story_id}:deleted",
                )
                return {"deleted": True, "story_id": story_id}
        if operation == "mark_story_read":
            story = story_by_id(params["story_id"], in_chat=chat_id)
            read = state.setdefault("read_stories", [])
            if story["story_id"] not in read:
                read.append(story["story_id"])
            commit()
            return {"story_id": story["story_id"], "read": True}
        if operation == "react_to_story":
            story = story_by_id(params["story_id"], in_chat=chat_id)
            state.setdefault("story_reactions", {})[story["story_id"]] = params["reaction"]
            story["reactions"] = story.get("reactions", 0) + 1
            commit()
            self._emit(
                account_id,
                "story.updated",
                None,
                {"story_id": story["story_id"], "state": "reacted"},
                f"story:{story['story_id']}:reacted:{uuid.uuid4().hex}",
            )
            return {"story_id": story["story_id"], "reacted": True}
        if operation == "pin_story":
            story = story_by_id(params["story_id"], owner_only=True)
            pins = state.setdefault("pinned_stories", [])
            if story["story_id"] in pins:
                pins.remove(story["story_id"])
                pinned = False
            else:
                pins.append(story["story_id"])
                pinned = True
            commit()
            return {"story_id": story["story_id"], "pinned": pinned}
        if operation == "archive_story":
            story = story_by_id(params["story_id"], owner_only=True)
            archived = state.setdefault("archived_stories", [])
            if story["story_id"] not in archived:
                archived.append(story["story_id"])
            story["state"] = "archived"
            commit()
            self._emit(
                account_id,
                "story.updated",
                None,
                {"story_id": story["story_id"], "state": "archived"},
                f"story:{story['story_id']}:archived",
            )
            return {"story_id": story["story_id"], "archived": True}
        if operation == "list_story_viewers":
            story = story_by_id(params["story_id"], in_chat=chat_id)
            viewers = list(story.get("viewers", []))[:100]
            return {"story_id": story["story_id"], "viewers": viewers, "complete": False}
        if operation == "report_story":
            story = story_by_id(params["story_id"], in_chat=chat_id)
            state.setdefault("reports", []).append(
                {
                    "target_ref": story["story_id"],
                    "chat_ref": chat_id,
                    "reason": params["reason"],
                    "kind": "story",
                    "details_recorded": False,
                }
            )
            commit()
            return {"recorded": True, "story_id": story["story_id"], "synthetic": True}
        if operation == "get_business_settings":
            return copy.deepcopy(state.setdefault("business_settings", {}))
        if operation == "update_business_settings":
            settings = state.setdefault("business_settings", {})
            settings.update({key: value for key, value in params.items() if value is not None})
            commit()
            return {"updated": True, "settings": copy.deepcopy(settings)}
        if operation == "list_business_links":
            return {"links": copy.deepcopy(state.setdefault("business_links", []))}
        if operation == "create_business_link":
            link = {
                "link_id": f"business-link-{uuid.uuid4().hex[:12]}",
                "slug": f"synthetic-{uuid.uuid4().hex[:8]}",
                **copy.deepcopy(params),
            }
            state.setdefault("business_links", []).append(link)
            commit()
            return {"link": copy.deepcopy(link)}
        if operation == "update_business_link":
            link = next(
                (
                    row
                    for row in state.setdefault("business_links", [])
                    if row["link_id"] == params["link_id"]
                ),
                None,
            )
            if link is None:
                raise AdapterError("not_found")
            link.update({key: value for key, value in params.items() if key != "link_id"})
            commit()
            return {"link": copy.deepcopy(link)}
        if operation == "delete_business_link":
            links = state.setdefault("business_links", [])
            link = next((row for row in links if row["link_id"] == params["link_id"]), None)
            if link is None:
                raise AdapterError("not_found")
            links.remove(link)
            commit()
            return {"deleted": True, "link_id": params["link_id"]}
        if operation == "resolve_business_link":
            link = next(
                (
                    row
                    for row in state.setdefault("business_links", [])
                    if row["slug"] == params["slug"]
                ),
                None,
            )
            if link is None:
                raise AdapterError("not_found")
            return {"link": copy.deepcopy(link)}
        if operation == "get_stars":
            stars = state.setdefault("stars", {})
            return {"balance": stars.get("balance", 0), "currency": "XTR", "synthetic": True}
        if operation == "list_gifts":
            gifts = state.setdefault("gifts", [])
            query = params.get("query", "").casefold()
            return {
                "gifts": [copy.deepcopy(row) for row in gifts if query in row["title"].casefold()]
            }
        if operation == "save_gift":
            gift = next(
                (
                    row
                    for row in state.setdefault("gifts", [])
                    if row["gift_id"] == params["item_id"]
                ),
                None,
            )
            if gift is None:
                raise AdapterError("not_found")
            gift["saved"] = True
            commit()
            return {"saved": True, "gift_id": gift["gift_id"]}
        if operation == "get_star_transactions":
            stars = state.setdefault("stars", {})
            rows = stars.get("transactions", [])
            limit = params.get("limit", 50)
            return {"transactions": copy.deepcopy(rows[-limit:]), "complete": False}
        if operation == "get_star_topup_options":
            return {
                "options": list(state.setdefault("stars", {}).get("topup_options", [])),
                "purchasable": False,
            }
        if operation == "list_star_subscriptions":
            return {
                "subscriptions": copy.deepcopy(
                    state.setdefault("stars", {}).get("subscriptions", [])
                )
            }
        if operation == "get_boost_status":
            return {
                "boost_count": len(state.get("boosters", [])),
                "boosters_visible": True,
                "synthetic": True,
            }
        if operation == "get_boosters":
            rows = state.get("boosters", [])
            limit = params.get("limit", 100)
            return {"boosters": copy.deepcopy(rows[:limit]), "has_more": len(rows) > limit}
        if operation == "get_group_call":
            return copy.deepcopy(state.get("group_call", {"active": False, "participant_count": 0}))
        if operation == "get_group_call_participants":
            rows = state.get("group_call", {}).get("participants", [])
            limit = params.get("limit", 100)
            return {"participants": copy.deepcopy(rows[:limit]), "has_more": len(rows) > limit}
        if operation == "list_discussion_eligible_chats":
            linked = set(state.get("discussion_links", {}))
            return {
                "chats": [
                    {"chat_id": chat_id, "title": account.chats[chat_id].title}
                    for chat_id in sorted(linked)
                    if chat_id in account.chats
                ]
            }
        if operation == "get_discussion":
            message(params["message_id"])
            return {
                "chat_id": state.get("discussion_links", {}).get(chat_id),
                "available": chat_id in state.get("discussion_links", {}),
            }
        if operation == "get_stats":
            return {
                "message_count": len(account.messages[chat_id]),
                "member_count": len(account.members.get(chat_id, {})),
                "unread": self._unread_summary(account, chat_id),
                "synthetic": True,
            }
        if operation == "list_quick_replies":
            return {
                "quick_replies": [
                    {"shortcut": key, "text": value}
                    for key, value in sorted(state.setdefault("quick_replies", {}).items())
                ]
            }
        if operation == "send_quick_reply":
            shortcut = params["shortcut"]
            text = state.setdefault("quick_replies", {}).get(shortcut)
            if text is None:
                raise AdapterError("not_found")
            message_id = account.next_id
            account.next_id += 1
            account.messages[chat_id].append(
                {"id": message_id, "chat_id": chat_id, "text": text, "media": None}
            )
            commit()
            self._emit_message(account_id, "message.new", chat_id, message_id, text)
            return {"message_id": message_id, "shortcut": shortcut}
        if operation == "save_quick_reply":
            state.setdefault("quick_replies", {})[params["shortcut"]] = params["text"]
            commit()
            return {"saved": True, "shortcut": params["shortcut"]}
        if operation == "delete_quick_reply":
            replies = state.setdefault("quick_replies", {})
            if params["shortcut"] not in replies:
                raise AdapterError("not_found")
            del replies[params["shortcut"]]
            commit()
            return {"deleted": True, "shortcut": params["shortcut"]}
        if operation == "query_inline_bot":
            if params["bot_username"].casefold() != "synthetic_demo_bot":
                raise AdapterError("unsupported_platform")
            return {
                "bot_username": params["bot_username"],
                "query_id": f"inline-{uuid.uuid4().hex[:12]}",
                "results": [{"result_id": "synthetic-inline-1", "text": "Synthetic inline result"}],
            }
        if operation == "send_inline_result":
            if params["result_id"] != "synthetic-inline-1":
                raise AdapterError("not_found")
            message_id = account.next_id
            account.next_id += 1
            account.messages[chat_id].append(
                {
                    "id": message_id,
                    "chat_id": chat_id,
                    "text": params["text"],
                    "media": None,
                    "inline_result_id": params["result_id"],
                }
            )
            commit()
            self._emit_message(account_id, "message.new", chat_id, message_id, params["text"])
            return {"message_id": message_id}
        if operation == "get_web_page_preview":
            message(params["message_id"])
            preview = state.setdefault("web_previews", {}).get(f"{chat_id}:{params['message_id']}")
            if preview is None:
                return {"available": False, "preview": None}
            return {
                "available": True,
                "preview": copy.deepcopy(preview),
                "fetched_by_gateway": False,
            }
        if operation == "get_profile_photo":
            photo = state.get("profile_photo")
            if photo is None:
                return {"available": False, "photo": None}
            result = copy.deepcopy(photo)
            result.pop("created_at", None)
            if "data_base64" not in result:
                result.pop("data_base64", None)
                return {"available": False, "photo": result}
            return {"available": True, "photo": result}
        if operation == "set_profile_photo":
            photo = self._validated_feature_media(params)
            state["profile_photo"] = {**photo, "created_at": self._clock()}
            commit()
            return {"updated": True}
        if operation == "delete_profile_photo":
            state["profile_photo"] = None
            commit()
            return {"deleted": True}
        raise AdapterError("unsupported_operation")

    @staticmethod
    def _unread_summary(account: _Account, chat_id: str) -> dict[str, int]:
        read_to = account.features.setdefault("read_up_to", {}).get(chat_id, 0)
        rows = [row for row in account.messages.get(chat_id, []) if row["id"] > read_to]
        mentions = sum(1 for row in rows if "@self" in row.get("text", ""))
        reactions = sum(
            1
            for key, value in account.features.setdefault("reactions", {}).items()
            if key.startswith(f"{chat_id}:") and value
        )
        return {
            "unread_count": len(rows),
            "unread_mentions": mentions,
            "unread_reactions": reactions,
        }

    @staticmethod
    def _validated_feature_media(value: dict[str, Any]) -> dict[str, Any]:
        filename = value.get("filename")
        content_type = value.get("content_type")
        encoded = value.get("data_base64")
        if (
            not isinstance(filename, str)
            or not filename
            or filename in {".", ".."}
            or "/" in filename
            or "\\" in filename
            or filename.startswith(".")
            or not isinstance(content_type, str)
            or not isinstance(encoded, str)
        ):
            raise AdapterError("unsupported_operation")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error, TypeError):
            raise AdapterError("unsupported_operation") from None
        if len(data) > 1_048_576:
            raise AdapterError("limit_exceeded")
        mime = content_type.strip().lower()
        signatures = {
            "text/plain": (),
            "text/csv": (),
            "application/json": (),
            "application/pdf": (b"%PDF-",),
            "application/zip": (b"PK\x03\x04",),
            "image/png": (b"\x89PNG\r\n\x1a\n",),
            "image/jpeg": (b"\xff\xd8\xff",),
            "image/gif": (b"GIF87a", b"GIF89a"),
            "image/webp": (b"RIFF",),
            "audio/ogg": (b"OggS",),
            "audio/mpeg": (b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"),
            "audio/mp4": (),
            "audio/webm": (b"\x1a\x45\xdf\xa3",),
            "video/mp4": (),
            "video/webm": (b"\x1a\x45\xdf\xa3",),
        }
        accepted = signatures.get(mime)
        if accepted is None:
            raise AdapterError("unsupported_operation")
        if mime in {"audio/mp4", "video/mp4"}:
            valid_signature = len(data) >= 8 and data[4:8] == b"ftyp"
        elif mime == "image/webp":
            valid_signature = len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
        elif accepted:
            valid_signature = any(data.startswith(signature) for signature in accepted)
        else:
            valid_signature = b"\x00" not in data[:512]
        if not valid_signature:
            raise AdapterError("unsupported_operation")
        return {
            "filename": filename,
            "content_type": mime,
            "data_base64": base64.b64encode(data).decode("ascii"),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
