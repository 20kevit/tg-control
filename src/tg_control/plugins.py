"""Plugin manifests, capability grants and failure containment (first-party only).

Design notes (see ADR-012):
- Only first-party, code-reviewed modules are installable. There is no remote
  marketplace, no arbitrary third-party import and no dynamic code loading. A
  manifest names an entry point that a trusted module registers; installation
  never imports or executes code.
- A manifest is declarative and strictly validated: known capabilities and
  extension points only, semver version, bounded budgets, no admin/policy-bypass
  capability, no inline credentials (only ``$secret:name`` references), and
  extension points must be consistent with the declared capabilities.
- Installation never enables anything. The owner must grant each capability
  explicitly, and a plugin runs under one named client's policy. Every action is
  re-authorized by Gateway Core, exactly as for an ordinary client.
- Runtime containment: bounded timeout, per-plugin rate limit, structured
  failure counters and quarantine after repeated failures. A misbehaving plugin
  cannot degrade Core, other plugins or policy enforcement.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import defaultdict, deque
from enum import StrEnum
from typing import Annotated, Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select

from . import audit
from .auth import Principal
from .core import BatchRejected, Gateway
from .db import Credential
from .observability import record_component_outcome
from .schemas import Action, Batch, History

PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,63}$")
ENTRY_POINT_RE = re.compile(r"^[a-z0-9_.]+:[A-Za-z_][A-Za-z0-9_.]*$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SECRET_FIELD_HINTS = ("token", "secret", "password", "api_key", "credential", "hash")
QUARANTINE_AFTER_FAILURES = 3


class PluginError(Exception):
    def __init__(self, code: str, status_code: int):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


class PluginCapability(StrEnum):
    READ = "read"
    WRITE = "write"
    MEDIA = "media"
    EVENTS = "events"
    SCHEDULING = "scheduling"
    AI_TASK = "ai_task"
    COMMAND = "command"


class ExtensionPoint(StrEnum):
    COMMAND = "command"
    JOB_HANDLER = "job_handler"
    EVENT_CONSUMER = "event_consumer"
    AI_TASK = "ai_task"
    MEDIA_PROCESSOR = "media_processor"
    WORKER = "worker"


EXTENSION_REQUIREMENTS: dict[ExtensionPoint, PluginCapability] = {
    ExtensionPoint.COMMAND: PluginCapability.COMMAND,
    ExtensionPoint.JOB_HANDLER: PluginCapability.SCHEDULING,
    ExtensionPoint.EVENT_CONSUMER: PluginCapability.EVENTS,
    ExtensionPoint.AI_TASK: PluginCapability.AI_TASK,
    ExtensionPoint.MEDIA_PROCESSOR: PluginCapability.MEDIA,
    ExtensionPoint.WORKER: PluginCapability.WRITE,
}
CAPABILITIES_REQUIRING_CLIENT = frozenset(
    {
        PluginCapability.WRITE,
        PluginCapability.MEDIA,
        PluginCapability.EVENTS,
        PluginCapability.SCHEDULING,
        PluginCapability.COMMAND,
    }
)


class PluginBudgets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_actions_per_minute: Annotated[int, Field(ge=1, le=600)] = 30
    action_timeout_seconds: Annotated[float, Field(ge=0.1, le=60.0)] = 5.0
    max_output_bytes: Annotated[int, Field(ge=256, le=1_048_576)] = 65_536


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plugin_id: str
    display_name: Annotated[str, Field(min_length=1, max_length=80)]
    publisher: Annotated[str, Field(min_length=1, max_length=80)]
    version: str
    api_version: Literal["1"] = "1"
    sdk_range: Annotated[str, Field(pattern=r"^[<>=~^0-9., ]{1,32}$")]
    entry_point: str
    description: Annotated[str, Field(max_length=500)] = ""
    extension_points: list[ExtensionPoint]
    capabilities: list[PluginCapability]
    client_id: uuid.UUID | None = None
    account_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)
    budgets: PluginBudgets = Field(default_factory=PluginBudgets)
    config: dict[str, Any] = Field(default_factory=dict, max_length=32)
    health_check: bool = True

    @field_validator("version")
    @classmethod
    def valid_version(cls, value: str) -> str:
        if not VERSION_RE.match(value):
            raise ValueError("version must be semver x.y.z")
        return value

    @field_validator("sdk_range")
    @classmethod
    def bounds_sdk_range(cls, value: str) -> str:
        major = re.findall(r"\d+", value)
        if not major or int(major[0]) != 1:
            raise ValueError("unsupported sdk range")
        return value

    @field_validator("plugin_id")
    @classmethod
    def valid_plugin_id(cls, value: str) -> str:
        if not PLUGIN_ID_RE.match(value):
            raise ValueError("invalid plugin id")
        if ".." in value or value.endswith("."):
            raise ValueError("invalid plugin id")
        return value

    @field_validator("entry_point")
    @classmethod
    def valid_entry_point(cls, value: str) -> str:
        if not ENTRY_POINT_RE.match(value):
            raise ValueError("invalid entry point")
        return value

    @field_validator("config")
    @classmethod
    def no_inline_secrets(cls, value: dict[str, Any]) -> dict[str, Any]:
        for key, item in value.items():
            if any(hint in key.lower() for hint in SECRET_FIELD_HINTS):
                if not (isinstance(item, str) and item.startswith("$secret:")):
                    raise ValueError("inline secrets are not accepted; use $secret:name")
            if isinstance(item, (dict, list)):
                raise ValueError("configuration must be flat scalars or $secret references")
            if isinstance(item, str) and len(item) > 512:
                raise ValueError("configuration value too long")
        return value

    @field_validator("extension_points")
    @classmethod
    def known_extension_points(cls, value: list[ExtensionPoint]) -> list[ExtensionPoint]:
        if not value:
            raise ValueError("at least one extension point is required")
        if len(set(value)) != len(value):
            raise ValueError("duplicate extension point")
        return value

    @field_validator("capabilities")
    @classmethod
    def known_capabilities(cls, value: list[PluginCapability]) -> list[PluginCapability]:
        if not value:
            raise ValueError("at least one capability is required")
        if len(set(value)) != len(value):
            raise ValueError("duplicate capability")
        return value

    @model_validator(mode="after")
    def coherent(self):
        self.validate_consistency()
        return self

    def validate_consistency(self) -> None:
        for point in self.extension_points:
            required = EXTENSION_REQUIREMENTS[point]
            if required not in self.capabilities:
                raise ValueError(f"extension point {point.value} requires {required.value}")
        if CAPABILITIES_REQUIRING_CLIENT & set(self.capabilities) and not self.client_id:
            raise ValueError("this capability set requires a delegated client")

    def as_dict(self) -> dict[str, Any]:
        return {
            "plugin_id": self.plugin_id,
            "display_name": self.display_name,
            "publisher": self.publisher,
            "version": self.version,
            "api_version": self.api_version,
            "sdk_range": self.sdk_range,
            "description": self.description,
            "extension_points": [point.value for point in self.extension_points],
            "capabilities": [cap.value for cap in self.capabilities],
            "client_id": str(self.client_id) if self.client_id else None,
            "account_ids": [str(item) for item in self.account_ids],
            "budgets": self.budgets.model_dump(),
            "config_keys": sorted(self.config),
            "health_check": self.health_check,
        }


class PluginState(StrEnum):
    INSTALLED = "installed"
    ENABLED = "enabled"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


class PluginRuntime:
    """Capability-checked surface handed to a plugin module."""

    def __init__(self, registry: "PluginRegistry", plugin_id: str):
        self._registry = registry
        self._plugin_id = plugin_id

    async def execute(self, batch: Batch, *, principal: Principal | None = None) -> dict:
        return await self._registry._call(self._plugin_id, "execute", batch, principal=principal)

    async def history(self, chat_id: str, limit: int = 20) -> dict[str, Any]:
        return await self._registry._call(
            self._plugin_id, "history", {"chat_id": chat_id, "limit": limit}
        )


class PluginRegistry:
    def __init__(
        self,
        sessions,
        gateway: Gateway,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_inflight: int = 8,
    ):
        if type(max_inflight) is not int or not 1 <= max_inflight <= 64:
            raise ValueError("invalid plugin concurrency limit")
        self.sessions = sessions
        self.gateway = gateway
        self._clock = clock
        self.max_inflight = max_inflight
        self._inflight = 0
        self._plugins: dict[str, dict[str, Any]] = {}
        self._grants: dict[str, set[PluginCapability]] = defaultdict(set)
        self._windows: dict[str, deque[float]] = defaultdict(deque)
        self._failures: dict[str, int] = defaultdict(int)

    # -- lifecycle ---------------------------------------------------------

    def install(self, manifest: PluginManifest) -> dict[str, Any]:
        if not isinstance(manifest, PluginManifest):
            raise PluginError("invalid_manifest", 422)
        try:
            manifest.validate_consistency()
        except ValueError as exc:
            raise PluginError(str(exc), 422) from None
        try:
            manifest.validate_consistency()
        except ValueError as exc:  # pragma: no cover - enforced by the model validator
            raise PluginError(str(exc), 422) from None
        existing = self._plugins.get(manifest.plugin_id)
        if existing is not None:
            current = existing["manifest"]
            if manifest.version == current.version:
                raise PluginError("duplicate_plugin", 409)
            if _version_tuple(manifest.version) <= _version_tuple(current.version):
                raise PluginError("downgrade_rejected", 422)
            if not set(current.capabilities).issubset(set(manifest.capabilities)):
                raise PluginError("capability_reduction_rejected", 422)
            # An upgrade keeps the plugin installed but requires re-enabling and
            # re-granting newly declared capabilities.
            self._grants[manifest.plugin_id] &= set(manifest.capabilities)
        self._plugins[manifest.plugin_id] = {
            "manifest": manifest,
            "state": PluginState.INSTALLED,
            "installed_at": self._clock(),
            "module": None,
        }
        self._audit(manifest.plugin_id, "plugin_install", "success")
        return self.get(manifest.plugin_id)

    def bind(self, plugin_id: str, module: Any) -> None:
        """Attach a first-party module instance. Never imports or executes code."""
        entry = self._require(plugin_id)
        declared = entry["manifest"].entry_point.split(":")[-1]
        if not hasattr(module, "plugin_id") or getattr(module, "plugin_id") != plugin_id:
            raise PluginError("module_mismatch", 422)
        if declared and not callable(getattr(module, declared.rsplit(".", 1)[-1], None)):
            raise PluginError("module_mismatch", 422)
        entry["module"] = module

    def grant(self, plugin_id: str, capabilities: list[PluginCapability]) -> dict[str, Any]:
        entry = self._require(plugin_id)
        declared = set(entry["manifest"].capabilities)
        requested = {PluginCapability(cap) for cap in capabilities}
        if not requested:
            raise PluginError("empty_grant", 422)
        if not requested.issubset(declared):
            raise PluginError("capability_not_declared", 422)
        self._grants[plugin_id] |= requested
        self._audit(plugin_id, "plugin_grant", "success")
        return self.get(plugin_id)

    def enable(self, plugin_id: str) -> dict[str, Any]:
        entry = self._require(plugin_id)
        if entry["module"] is None:
            raise PluginError("module_not_bound", 409)
        missing = set(entry["manifest"].capabilities) - self._grants[plugin_id]
        if missing:
            raise PluginError("capability_not_granted", 403)
        entry["state"] = PluginState.ENABLED
        self._failures[plugin_id] = 0
        self._audit(plugin_id, "plugin_enable", "success")
        return self.get(plugin_id)

    def disable(self, plugin_id: str) -> dict[str, Any]:
        entry = self._require(plugin_id)
        entry["state"] = PluginState.DISABLED
        self._audit(plugin_id, "plugin_disable", "success")
        return self.get(plugin_id)

    def status(self, plugin_id: str) -> dict[str, Any]:
        return self.get(plugin_id)

    def list(self) -> list[dict[str, Any]]:
        return [self.get(plugin_id) for plugin_id in sorted(self._plugins)]

    def runtime(self, plugin_id: str) -> PluginRuntime:
        self._require(plugin_id)
        return PluginRuntime(self, plugin_id)

    def get(self, plugin_id: str) -> dict[str, Any]:
        entry = self._require(plugin_id)
        return {
            **entry["manifest"].as_dict(),
            "state": entry["state"].value,
            "granted_capabilities": sorted(cap.value for cap in self._grants[plugin_id]),
            "consecutive_failures": self._failures[plugin_id],
            "module_bound": entry["module"] is not None,
        }

    # -- containment -------------------------------------------------------

    async def _call(
        self,
        plugin_id: str,
        operation: Literal["execute", "history"],
        payload: Any,
        *,
        principal: Principal | None = None,
    ) -> Any:
        if self._inflight >= self.max_inflight:
            raise PluginError("plugin_busy", 429)
        entry = self._require(plugin_id)
        manifest: PluginManifest = entry["manifest"]
        if entry["state"] != PluginState.ENABLED:
            raise PluginError("plugin_not_enabled", 409)
        required = (
            PluginCapability.WRITE
            if operation == "execute"
            else PluginCapability.READ
        )
        if manifest.client_id is not None and required not in self._grants[plugin_id]:
            raise PluginError("capability_denied", 403)
        if principal is not None and principal.client_id != str(manifest.client_id):
            # A plugin may never act for a different client than the delegated one.
            raise PluginError("principal_mismatch", 403)
        self._rate_limit(plugin_id, manifest.budgets.max_actions_per_minute)
        if principal is not None:
            scoped = principal
        else:
            with self.sessions() as session:
                credential_id = session.scalar(
                    select(Credential.id)
                    .where(
                        Credential.client_id == str(manifest.client_id),
                        Credential.revoked.is_(False),
                    )
                    .order_by(Credential.id)
                )
            if credential_id is None:
                raise PluginError("client_not_active", 409)
            scoped = Principal(
                client_id=str(manifest.client_id), credential_id=credential_id
            )
        if operation == "history" and not manifest.account_ids:
            raise PluginError("account_not_declared", 422)
        batch = payload
        if operation == "history":
            action: Action = History(
                operation="history",
                account_id=manifest.account_ids[0],
                chat_id=payload["chat_id"],
                limit=payload["limit"],
            )
            batch = Batch(actions=[action])
        self._inflight += 1
        try:
            result = await asyncio.wait_for(
                self.gateway.execute(scoped, batch),
                timeout=manifest.budgets.action_timeout_seconds,
            )
        except (TimeoutError, asyncio.TimeoutError):
            self._record_failure(plugin_id, "plugin_timeout")
            raise PluginError("plugin_timeout", 504) from None
        except BatchRejected as exc:
            code = exc.code
            if exc.status_code == 403 and exc.items and exc.items[0].get("error") == "denied":
                code = "denied"
            raise PluginError(code, exc.status_code) from None
        except PluginError:
            raise
        except Exception:
            self._record_failure(plugin_id, "plugin_error")
            raise PluginError("plugin_error", 502) from None
        finally:
            self._inflight = max(0, self._inflight - 1)
        self._failures[plugin_id] = 0
        record_component_outcome("plugins", "success", kind=operation)
        self._audit(plugin_id, f"plugin_{operation}", "success")
        if operation == "history":
            return result["items"][0].get("data", {})
        return result

    def _rate_limit(self, plugin_id: str, limit: int) -> None:
        now = self._clock()
        window = self._windows[plugin_id]
        while window and now - window[0] >= 60:
            window.popleft()
        if len(window) >= limit:
            self._audit(plugin_id, "plugin_action", "rate_limited")
            raise PluginError("plugin_rate_limited", 429)
        window.append(now)

    def _record_failure(self, plugin_id: str, error: str) -> None:
        record_component_outcome("plugins", "failure", kind="action")
        self._failures[plugin_id] += 1
        entry = self._plugins.get(plugin_id)
        if entry is not None and self._failures[plugin_id] >= QUARANTINE_AFTER_FAILURES:
            entry["state"] = PluginState.QUARANTINED
            self._audit(plugin_id, "plugin_quarantine", "failure")
        self._audit(plugin_id, "plugin_action", error)

    def _require(self, plugin_id: str) -> dict[str, Any]:
        entry = self._plugins.get(plugin_id)
        if entry is None:
            raise PluginError("plugin_not_found", 404)
        return entry

    def _audit(self, plugin_id: str, event: str, decision: str) -> None:
        with self.sessions() as session:
            audit.record(
                session,
                request_id=str(uuid.uuid4()),
                actor_kind="owner",
                actor_id=None,
                event=event,
                decision=decision,
                error_code=f"plugin:{plugin_id}" if decision != "success" else None,
            )
            session.commit()


def _version_tuple(version: str) -> tuple[int, int, int]:
    major, minor, patch = version.split(".")
    return int(major), int(minor), int(patch)
