"""Opt-in automatic retention pruning.

The owner-only `POST /v1/admin/maintenance/prune-*` endpoints stay authoritative and
unchanged. This module adds a *scheduled* variant so a long-running gateway can
bound its own growth without an external cron:

* disabled by default (`auto_prune: false` in the stored retention settings);
* at most one run per configured interval (60–10080 minutes, default 1440);
* only runs while this process holds the single-instance guard;
* reuses the existing bounded prune paths, so non-terminal jobs and running
  workflow runs are never eligible;
* attempts one metadata-only audit row per run (counts and outcomes are
  never written to audit, which stores metadata only);
* one failing step does not abort the others; audit is attempted and failures
  are reported in diagnostics.

Nothing here deletes credentials, audit of the run itself, configurable state or
media bytes; media retention stays on the adapter path.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

MIN_INTERVAL_MINUTES = 60
MAX_INTERVAL_MINUTES = 10_080
DEFAULT_INTERVAL_MINUTES = 1_440
DEFAULT_HISTORY_DAYS = 90
MAX_RETENTION_DAYS = 36_500


@dataclass(frozen=True)
class AutoPrunePolicy:
    """Validated schedule+window for one automatic pruning run."""

    enabled: bool = False
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    audit_retention_days: int = 90
    event_retention_days: int = 30
    history_retention_days: int = DEFAULT_HISTORY_DAYS

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("auto_prune.enabled must be a boolean")
        for name, value, low in (
            ("interval_minutes", self.interval_minutes, MIN_INTERVAL_MINUTES),
            ("audit_retention_days", self.audit_retention_days, 1),
            ("event_retention_days", self.event_retention_days, 1),
            ("history_retention_days", self.history_retention_days, 1),
        ):
            if type(value) is not int or not low <= value <= (
                MAX_INTERVAL_MINUTES if name == "interval_minutes" else MAX_RETENTION_DAYS
            ):
                raise ValueError(f"invalid auto-prune {name}")

    @property
    def interval_seconds(self) -> int:
        return self.interval_minutes * 60


class RetentionRunner:
    """Runs the documented prune paths on a bounded schedule."""

    def __init__(
        self,
        *,
        sessions: Callable[[], Any],
        audit_module: Any,
        jobs: Any,
        workflows: Any,
        events: Any,
        load_policy: Callable[[], AutoPrunePolicy],
        clock: Callable[[], float] = time.monotonic,
        guard: Callable[[], bool] | None = None,
    ) -> None:
        self._sessions = sessions
        self._audit = audit_module
        self._jobs = jobs
        self._workflows = workflows
        self._events = events
        self._load_policy = load_policy
        self._clock = clock
        self._guard = guard
        # A fresh process waits a full interval before its first automatic run:
        # startup stays predictable and a short-lived process never prunes.
        self._last_run: float | None = self._clock()
        self._last_result: dict[str, Any] | None = None

    # -- scheduling -------------------------------------------------------

    def policy(self) -> AutoPrunePolicy:
        policy = self._load_policy()
        if not isinstance(policy, AutoPrunePolicy):
            raise ValueError("invalid auto-prune policy")
        return policy

    def is_due(self, *, now: float | None = None) -> bool:
        policy = self.policy()
        if not policy.enabled:
            return False
        if self._guard is not None and not self._guard():
            return False
        elapsed = (now if now is not None else self._clock()) - (
            self._last_run if self._last_run is not None else 0.0
        )
        return elapsed >= policy.interval_seconds

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        """Operator-visible state for diagnostics; no payloads or identifiers."""
        try:
            policy = self.policy()
        except Exception:
            return {"state": "invalid", "enabled": False, "last_run_age_seconds": None}
        current = now if now is not None else self._clock()
        age = None if self._last_run is None else max(0, int(current - self._last_run))
        return {
            "state": "enabled" if policy.enabled else "disabled",
            "enabled": policy.enabled,
            "interval_minutes": policy.interval_minutes,
            "last_run_age_seconds": age,
            "last_run_outcome": (self._last_result or {}).get("outcome"),
        }

    async def run_if_due(self, *, now: float | None = None) -> dict[str, Any] | None:
        if not self.is_due(now=now):
            return None
        policy = self.policy()
        result = self.run_once(policy)
        self._last_run = now if now is not None else self._clock()
        self._last_result = result
        return result

    # -- execution --------------------------------------------------------

    def run_once(self, policy: AutoPrunePolicy) -> dict[str, Any]:
        """Run every prune step once; never raises for a single failing step."""
        deleted: dict[str, int] = {}
        failed: list[str] = []
        try:
            with self._sessions() as session:
                deleted["audit"] = int(
                    self._audit.prune(session, policy.audit_retention_days) or 0
                )
                session.commit()
        except Exception:
            failed.append("audit")
        try:
            deleted["events"] = int(self._events.prune(policy.event_retention_days) or 0)
        except Exception:
            failed.append("events")
        try:
            jobs_result = self._jobs.prune_terminal(
                older_than_days=policy.history_retention_days, dry_run=False
            )
            deleted["jobs"] = int(jobs_result.get("deleted", 0))
        except Exception:
            failed.append("jobs")
        try:
            workflows_result = self._workflows.prune_runs(
                older_than_days=policy.history_retention_days, dry_run=False
            )
            deleted["workflows"] = int(workflows_result.get("deleted", 0))
        except Exception:
            failed.append("workflows")
        outcome = "failure" if failed else "success"
        if not self._record(outcome, failed):
            failed.append("audit_record")
            outcome = "failure"
        return {
            "state": "pruned",
            "outcome": outcome,
            "deleted": deleted,
            "failed_steps": failed,
            "retention_days": {
                "audit": policy.audit_retention_days,
                "events": policy.event_retention_days,
                "history": policy.history_retention_days,
            },
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }

    def _record(self, outcome: str, failed: list[str]) -> bool:
        """Attempt one metadata-only audit row; report write or commit failure."""
        try:
            with self._sessions() as session:
                self._audit.record(
                    session,
                    request_id=str(uuid.uuid4()),
                    actor_kind="system",
                    actor_id=None,
                    event="retention_auto_prune",
                    decision="allow" if outcome == "success" else "deny",
                    error_code="retention_failed" if failed else None,
                )
                session.commit()
            return True
        except Exception:
            # Do not crash maintenance or expose exception text.
            return False
