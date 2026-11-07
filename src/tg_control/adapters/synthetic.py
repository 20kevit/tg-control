"""Synthetic account simulator. It cannot connect to Telegram."""

from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .base import AdapterError, Chat, ChatKind, SessionStatus, SessionUnavailable, Update
from .synthetic_store import SyntheticStore


@dataclass
class _Account:
    chats: dict[str, Chat]
    messages: dict[str, list[dict[str, Any]]]
    state: str = "ready"
    next_id: int = 10
    members: dict[str, dict[str, str]] = field(default_factory=dict)


def _fixture(account_id: str, timestamp: float) -> _Account:
    chats = {
        "public-channel": Chat("public-channel", ChatKind.PUBLIC_CHANNEL, "Synthetic channel"),
        "public-group": Chat("public-group", ChatKind.PUBLIC_GROUP, "Synthetic public group"),
        "private-group": Chat("private-group", ChatKind.PRIVATE_GROUP, "Synthetic private group"),
        "private-direct": Chat("private-direct", ChatKind.PRIVATE_DIRECT, "Synthetic direct chat"),
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
    return _Account(chats, messages)


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
    }


def _decode(data: dict) -> _Account:
    chats = {
        key: Chat(value["id"], ChatKind(value["kind"]), value["title"])
        for key, value in data["chats"].items()
    }
    return _Account(chats, data["messages"], data["state"], data["next_id"], data["members"])


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

    async def close(self) -> None:
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
        return count

    async def configure_media_retention(self, days: int | None) -> None:
        if days is not None and days < 1:
            raise ValueError("invalid media retention")
        async with self._lock:
            self._media_retention_days = days

    async def prune_media(self, account_id: str) -> int:
        async with self._lock:
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
            raise ValueError("invalid update drain limit")
        async with self._lock:
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
            state = self._account(account_id).state
            return SessionStatus(state, state == "reauth_required")

    async def reconnect(self, account_id: str) -> SessionStatus:
        async with self._lock:
            account = copy.deepcopy(self._account(account_id))
            if account.state == "reauth_required":
                raise SessionUnavailable(True)
            account.state = "ready"
            self._commit(account_id, account)
            return SessionStatus("ready")

    def _check(self, account_id: str, operation: str) -> _Account:
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

    async def execute(
        self, account_id: str, operation: str, chat_id: str, params: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            account = copy.deepcopy(self._check(account_id, operation))
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
                new_id = account.next_id
                account.next_id += 1
                messages.append({"id": new_id, "chat_id": chat_id, "text": text, "media": media})
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
