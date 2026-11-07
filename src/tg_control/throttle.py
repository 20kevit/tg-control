"""Authentication-failure throttling (single-process, bounded, fail-closed).

The key is a digest of the transport source address, never a token, so a log or
metric can never reveal a credential. A blocked source receives `429` with
`Retry-After` before any credential comparison, which also removes the timing
difference between "unknown token" and "throttled" for the attacker's budget.

This is deliberately in-process: the gateway is single-instance (see
`instance_guard.py`). A distributed deployment would need a shared counter; that
requirement is not part of this architecture.
"""

from __future__ import annotations

import hashlib
import time
from collections import OrderedDict
from typing import Callable

DEFAULT_MAX_FAILURES = 20
DEFAULT_WINDOW_SECONDS = 60.0
DEFAULT_LOCKOUT_SECONDS = 60.0
MAX_TRACKED_SOURCES = 2048


class AuthThrottle:
    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        lockout_seconds: float = DEFAULT_LOCKOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not 1 <= max_failures <= 10_000:
            raise ValueError("invalid failure threshold")
        if not 1 <= window_seconds <= 3600:
            raise ValueError("invalid throttle window")
        if not 1 <= lockout_seconds <= 3600:
            raise ValueError("invalid lockout duration")
        self.max_failures = max_failures
        self.window_seconds = float(window_seconds)
        self.lockout_seconds = float(lockout_seconds)
        self._clock = clock
        self._state: OrderedDict[str, list[float]] = OrderedDict()

    @staticmethod
    def key(source: str | None) -> str:
        raw = (source or "unknown").encode("utf-8", "replace")[:128]
        return hashlib.sha256(b"tg-control:auth-throttle:" + raw).hexdigest()[:32]

    def retry_after(self, source: str | None) -> float | None:
        """Seconds until the source may try again, or None when it may proceed."""
        key = self.key(source)
        failures = self._state.get(key)
        if not failures:
            return None
        now = self._clock()
        horizon = max(self.window_seconds, self.lockout_seconds)
        failures[:] = [stamp for stamp in failures if now - stamp < horizon]
        if not failures:
            self._state.pop(key, None)
            return None
        self._state.move_to_end(key)
        if len(failures) < self.max_failures:
            return None
        blocking = failures[-self.max_failures]
        remaining = self.lockout_seconds - (now - blocking)
        return round(remaining, 3) if remaining > 0 else None

    def failure(self, source: str | None) -> None:
        key = self.key(source)
        now = self._clock()
        failures = self._state.get(key)
        if failures is None:
            failures = []
            self._state[key] = failures
        failures.append(now)
        del failures[:-self.max_failures]
        self._state.move_to_end(key)
        while len(self._state) > MAX_TRACKED_SOURCES:
            self._state.popitem(last=False)
        if self._state[key] and len(self._state[key]) >= self.max_failures:
            # Keep only the newest window once locked out to bound memory.
            self._state[key] = self._state[key][-self.max_failures:]

    def success(self, source: str | None) -> None:
        self._state.pop(self.key(source), None)

    def reset(self) -> None:
        self._state.clear()
