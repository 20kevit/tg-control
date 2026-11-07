"""Process-local, content-free progress metadata for the synthetic session monitor."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Callable


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SessionMonitorProgress:
    """Observations about the task, not proof of external/session health."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = _now,
    ) -> None:
        self.clock = clock
        self.wall_clock = wall_clock
        self.in_progress = False
        self.last_result = "never"
        self.consecutive_failures = 0
        self.last_started_at: datetime | None = None
        self.last_completed_at: datetime | None = None
        self.last_success_at: datetime | None = None
        self._current_started: float | None = None
        self._last_success: float | None = None

    def begin(self) -> None:
        self.in_progress = True
        self.last_started_at = self.wall_clock()
        self._current_started = self.clock()

    def finish(self, *, success: bool) -> None:
        self.in_progress = False
        self.last_completed_at = self.wall_clock()
        self._current_started = None
        if success:
            self.last_result = "success"
            self.consecutive_failures = 0
            self.last_success_at = self.last_completed_at
            self._last_success = self.clock()
        else:
            self.last_result = "failure"
            self.consecutive_failures += 1

    def snapshot(self, *, task_running: bool) -> dict:
        now = self.clock()

        def age(value: float | None) -> float | None:
            return round(max(0.0, now - value), 3) if value is not None else None

        def stamp(value: datetime | None) -> str | None:
            return value.isoformat() if value is not None else None

        return {
            "task_running": task_running,
            "pass_in_progress": self.in_progress,
            "last_result": self.last_result,
            "consecutive_failures": self.consecutive_failures,
            "last_started_at": stamp(self.last_started_at),
            "last_completed_at": stamp(self.last_completed_at),
            "last_success_at": stamp(self.last_success_at),
            "current_pass_seconds": age(self._current_started),
            "seconds_since_success": age(self._last_success),
        }
