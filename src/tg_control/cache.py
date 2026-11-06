"""Bounded, process-local cache. Authorization always runs before a cache lookup."""

from __future__ import annotations

import copy
import json
import time
from collections import OrderedDict
from typing import Callable


class ReadCache:
    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_entries: int = 1024,
        max_bytes: int = 16 * 1024 * 1024,
    ):
        self.clock = clock
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self.ttl = 0
        self._bytes = 0
        self._entries: OrderedDict[tuple, tuple[float, dict, int]] = OrderedDict()
        self._generation: dict[str, int] = {}

    @staticmethod
    def key(account_id: str, operation: str, chat_id: str, params: dict) -> tuple:
        # Search strings remain only in process memory; keys are never logged or persisted.
        return (account_id, operation, chat_id, json.dumps(params, sort_keys=True))

    def _remove(self, key: tuple) -> None:
        _, _, size = self._entries.pop(key)
        self._bytes -= size

    def configure(self, ttl: int) -> None:
        if ttl < 0:
            raise ValueError("invalid cache TTL")
        self.ttl = ttl
        self._entries.clear()
        self._bytes = 0
        # In-flight reads must not repopulate after a policy change.
        for account_id in self._generation:
            self._generation[account_id] += 1

    def generation(self, account_id: str) -> int:
        return self._generation.setdefault(account_id, 0)

    def get(self, key: tuple) -> dict | None:
        entry = self._entries.get(key)
        if not entry:
            return None
        created, value, _ = entry
        if not self.ttl or self.clock() - created >= self.ttl:
            self._remove(key)
            return None
        self._entries.move_to_end(key)
        return copy.deepcopy(value)

    def put(self, key: tuple, value: dict, generation: int) -> None:
        if not self.ttl or self.generation(key[0]) != generation:
            return
        # Bound aggregate content and key material, not just the number of entries.
        size = len(json.dumps([key, value], ensure_ascii=False).encode("utf-8"))
        if size > self.max_bytes:
            return
        if key in self._entries:
            self._remove(key)
        self._entries[key] = (self.clock(), copy.deepcopy(value), size)
        self._bytes += size
        while len(self._entries) > self.max_entries or self._bytes > self.max_bytes:
            oldest = next(iter(self._entries))
            self._remove(oldest)

    def invalidate(self, account_id: str) -> None:
        self._generation[account_id] = self.generation(account_id) + 1
        for key in list(self._entries):
            if key[0] == account_id:
                self._remove(key)

    def prune(self) -> None:
        now = self.clock()
        for key, (created, _, _) in list(self._entries.items()):
            if not self.ttl or now - created >= self.ttl:
                self._remove(key)
