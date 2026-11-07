"""Machine-readable cross-interface operation registry.

One row per externally reachable capability. The HTTP route, MCP tool and CLI
command are *transport bindings* of the same Gateway Core/Policy operation; no
interface implements business logic of its own. `tests/test_contracts.py`
enforces that this registry matches the running FastAPI app, the MCP tool list and
the CLI parser, and that `docs/contracts/interfaces.json` is current.

Fields:
- name: stable operation name (also used for docs and metrics `kind`).
- http: ``"METHOD /path"`` or None when a transport does not expose it.
- mcp_tool: MCP tool name or None.
- cli: CLI subcommand or None. ``request`` is a deliberate generic escape hatch
  and is documented as not part of the semantic parity guarantees.
- principal: owner, client or none (unauthenticated is not used anywhere).
- core: the Gateway Core operation or service entry that performs the work.
- errors: normalized error codes this operation can return.
- pagination: whether the operation accepts cursor pagination.
- audit: whether the operation writes an audit record.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

OWNER = "owner"
CLIENT = "client"
NONE = "none"


@dataclass(frozen=True)
class Operation:
    name: str
    principal: str
    core: str
    errors: tuple[str, ...]
    http: str | None = None
    mcp_tool: str | None = None
    cli: str | None = None
    pagination: bool = False
    audit: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "http": self.http,
            "mcp_tool": self.mcp_tool,
            "cli": self.cli,
            "principal": self.principal,
            "core": self.core,
            "errors": list(self.errors),
            "pagination": self.pagination,
            "audit": self.audit,
        }


CORE_ERRORS = ("invalid_request", "invalid_credential", "credential_revoked", "denied")
EXECUTE_ERRORS = (
    *CORE_ERRORS,
    "batch_precondition_failed",
    "idempotency_conflict",
    "rate_limit",
    "storage_unavailable",
)
ADMIN_ERRORS = ("invalid_request", "invalid_credential", "forbidden", "conflict")
PAGINATION_ERRORS = ("invalid_cursor", "expired_cursor")

OPERATIONS: tuple[Operation, ...] = (
    # -- client plane: every operation is exposed on HTTP, MCP and CLI -----
    Operation("accounts.list", CLIENT, "Gateway.list_accounts", CORE_ERRORS,
              "GET /v1/accounts", "tg_accounts", "accounts"),
    Operation("actions.execute", CLIENT, "Gateway.execute", EXECUTE_ERRORS,
              "POST /v1/execute", "tg_execute", "execute"),
    Operation("jobs.create", CLIENT, "Jobs.enqueue",
              (*CORE_ERRORS, "unknown_job_kind", "invalid_input", "conflict"),
              "POST /v1/jobs", "tg_jobs_create", "jobs-create"),
    Operation("jobs.list", CLIENT, "Jobs.list_jobs", (*CORE_ERRORS, *PAGINATION_ERRORS),
              "GET /v1/jobs", "tg_jobs", "jobs", pagination=True),
    Operation("jobs.inspect", CLIENT, "Jobs.inspect", (*CORE_ERRORS, "job_not_found"),
              "GET /v1/jobs/{job_id}", "tg_job_get", "jobs-get"),
    Operation("jobs.pause", CLIENT, "Jobs.set_paused",
              (*CORE_ERRORS, "job_not_found", "job_conflict"),
              "POST /v1/jobs/{job_id}/pause", "tg_job_pause", "jobs-pause"),
    Operation("jobs.resume", CLIENT, "Jobs.set_paused",
              (*CORE_ERRORS, "job_not_found", "job_conflict"),
              "POST /v1/jobs/{job_id}/resume", "tg_job_resume", "jobs-resume"),
    Operation("jobs.cancel", CLIENT, "Jobs.cancel",
              (*CORE_ERRORS, "job_not_found", "job_conflict"),
              "POST /v1/jobs/{job_id}/cancel", "tg_job_cancel", "jobs-cancel"),
    Operation("commands.catalog", CLIENT, "CommandRouter.catalog", CORE_ERRORS,
              "GET /v1/commands", "tg_commands", "commands"),
    Operation("commands.save", CLIENT, "CommandRouter.invoke",
              (*CORE_ERRORS, "unresolved_reply", "unavailable_source", "duplicate_invocation",
               "unsupported_content", "unsupported_media", "destination_denied", "rate_limit",
               "outcome_unknown"),
              "POST /v1/commands/save", "tg_save", "save"),
    Operation("events.subscribe", CLIENT, "EventService.create_subscription", CORE_ERRORS,
              "POST /v1/events/subscriptions", "tg_events_subscribe", "events-subscribe"),
    Operation("events.subscriptions", CLIENT, "EventService.list_subscriptions", CORE_ERRORS,
              "GET /v1/events/subscriptions", "tg_events_subscriptions", "events"),
    Operation("events.unsubscribe", CLIENT, "EventService.delete_subscription", CORE_ERRORS,
              "DELETE /v1/events/subscriptions/{subscription_id}", "tg_events_unsubscribe",
              "events-unsubscribe"),
    Operation("events.pause", CLIENT, "EventService.set_paused", CORE_ERRORS,
              "POST /v1/events/subscriptions/{subscription_id}/pause", "tg_events_pause",
              "events-pause"),
    Operation("events.resume", CLIENT, "EventService.set_paused", CORE_ERRORS,
              "POST /v1/events/subscriptions/{subscription_id}/resume", "tg_events_resume",
              "events-resume"),
    Operation("events.pull", CLIENT, "EventService.pull",
              (*CORE_ERRORS, "cursor_expired", "subscription_paused"),
              "POST /v1/events/subscriptions/{subscription_id}/pull", "tg_events_pull",
              "events-pull"),
    Operation("events.ack", CLIENT, "EventService.ack", (*CORE_ERRORS, "invalid_cursor"),
              "POST /v1/events/subscriptions/{subscription_id}/ack", "tg_events_ack",
              "events-ack"),
    Operation("events.nack", CLIENT, "EventService.nack", (*CORE_ERRORS, "event_not_found"),
              "POST /v1/events/subscriptions/{subscription_id}/nack", "tg_events_nack",
              "events-nack"),
    Operation("ai.catalog", CLIENT, "AIService.catalog", CORE_ERRORS,
              "GET /v1/ai/tasks", "tg_ai_tasks", "ai-tasks"),
    Operation("ai.run", CLIENT, "AIService.invoke",
              (*CORE_ERRORS, "unknown_task", "unknown_provider", "unknown_model",
               "unsupported_task", "compliance_blocked", "invalid_input", "input_too_large",
               "budget_exceeded", "timeout", "provider_error", "invalid_output"),
              "POST /v1/ai/tasks/{task_name}/run", "tg_ai_run", "ai-run"),
    # -- owner plane: HTTP-only by design (owner credential is not an MCP
    #    client credential and the CLI uses a client token). `cli` is set only
    #    where an owner command exists. -----------------------------------
    Operation("health.live", OWNER, "app.liveness", (),
              "GET /health/live", None, None),
    Operation("health.ready", OWNER, "app.readiness", (),
              "GET /health/ready", None, "health"),
    Operation("health.dependencies", OWNER, "app.dependencies", (),
              "GET /health/dependencies", None, "health-dependencies"),
    Operation("diagnostics.snapshot", OWNER, "Diagnostics.collect", ADMIN_ERRORS,
              "GET /v1/admin/diagnostics", None, "diagnose"),
    Operation("metrics.snapshot", OWNER, "observability.METRICS", ADMIN_ERRORS,
              "GET /v1/admin/metrics", None, "metrics"),
    Operation("traces.recent", OWNER, "observability.SPANS", ADMIN_ERRORS,
              "GET /v1/admin/traces", None, None),
    Operation("openapi.schema", OWNER, "FastAPI.openapi", (),
              "GET /openapi.json", None, None),
    Operation("audit.list", OWNER, "AuditEvent query",
              ("invalid_request", "invalid_credential", "forbidden", *PAGINATION_ERRORS),
              "GET /v1/admin/audit", None, "audit", pagination=True),
    Operation("admin.accounts.list", OWNER, "Account query", ADMIN_ERRORS,
              "GET /v1/admin/accounts", None, None),
    Operation("admin.accounts.create", OWNER, "Account provisioning", ADMIN_ERRORS,
              "POST /v1/admin/accounts", None, None),
    Operation("admin.accounts.update", OWNER, "Account update", ADMIN_ERRORS,
              "PATCH /v1/admin/accounts/{account_id}", None, None),
    Operation("admin.accounts.status", OWNER, "Gateway.owner_account_status", ADMIN_ERRORS,
              "GET /v1/admin/accounts/{account_id}/status", None, None),
    Operation("admin.accounts.reconnect", OWNER, "Gateway.owner_account_status", ADMIN_ERRORS,
              "POST /v1/admin/accounts/{account_id}/reconnect", None, None),
    Operation("admin.clients.list", OWNER, "Client query", ADMIN_ERRORS,
              "GET /v1/admin/clients", None, None),
    Operation("admin.clients.create", OWNER, "rotate_credential", ADMIN_ERRORS,
              "POST /v1/admin/clients", None, None),
    Operation("admin.clients.rotate", OWNER, "rotate_credential", ADMIN_ERRORS,
              "POST /v1/admin/clients/{client_id}/rotate", None, None),
    Operation("admin.clients.revoke", OWNER, "revoke_client", ADMIN_ERRORS,
              "POST /v1/admin/clients/{client_id}/revoke", None, None),
    Operation("admin.policy.get", OWNER, "policy.get_scope", ADMIN_ERRORS,
              "GET /v1/admin/clients/{client_id}/accounts/{account_id}/policy", None, None),
    Operation("admin.policy.set", OWNER, "Grant update", ADMIN_ERRORS,
              "PUT /v1/admin/clients/{client_id}/accounts/{account_id}/policy", None, None),
    Operation("admin.session_monitor", OWNER, "SessionMonitorProgress.snapshot", ADMIN_ERRORS,
              "GET /v1/admin/session-monitor", None, None),
    Operation("admin.storage_status", OWNER, "Gateway.storage_status", ADMIN_ERRORS,
              "GET /v1/admin/storage-status", None, None),
    Operation("admin.settings.get", OWNER, "Setting query", ADMIN_ERRORS,
              "GET /v1/admin/settings", None, None),
    Operation("admin.settings.put", OWNER, "Retention configuration", ADMIN_ERRORS,
              "PUT /v1/admin/settings", None, None),
    Operation("admin.maintenance.prune_audit", OWNER, "audit.prune", ADMIN_ERRORS,
              "POST /v1/admin/maintenance/prune-audit", None, None),
    Operation("admin.maintenance.prune_events", OWNER, "EventService.prune", ADMIN_ERRORS,
              "POST /v1/admin/maintenance/prune-events", None, None),
    Operation("admin.maintenance.prune_jobs", OWNER, "Jobs.prune_terminal", ADMIN_ERRORS,
              "POST /v1/admin/maintenance/prune-jobs", None, None),
    Operation("admin.maintenance.prune_workflows", OWNER, "Workflows.prune_runs", ADMIN_ERRORS,
              "POST /v1/admin/maintenance/prune-workflows", None, None),
    Operation("admin.events.dead_letters", OWNER, "EventService.dead_letters", ADMIN_ERRORS,
              "GET /v1/admin/events/dead-letters", None, None),
    Operation("admin.events.replay", OWNER, "EventService.replay_dead_letter", ADMIN_ERRORS,
              "POST /v1/admin/events/dead-letters/{dead_letter_id}/replay", None, None),
    Operation("admin.jobs.list", OWNER, "Jobs.list_jobs", (*ADMIN_ERRORS, *PAGINATION_ERRORS),
              "GET /v1/admin/jobs", None, None, pagination=True),
    Operation("admin.jobs.inspect", OWNER, "Jobs.inspect", ADMIN_ERRORS,
              "GET /v1/admin/jobs/{job_id}", None, None),
    Operation("admin.jobs.cancel", OWNER, "Jobs.cancel", ADMIN_ERRORS,
              "POST /v1/admin/jobs/{job_id}/cancel", None, None),
    Operation("admin.jobs.tick", OWNER, "Jobs.tick", ADMIN_ERRORS,
              "POST /v1/admin/jobs/tick", None, None),
    Operation("admin.jobs.recover", OWNER, "Jobs.recover_expired_leases", ADMIN_ERRORS,
              "POST /v1/admin/jobs/recover", None, None),
    Operation("admin.commands.save", OWNER, "CommandRouter.invoke", ADMIN_ERRORS,
              "POST /v1/admin/commands/save", None, None),
    Operation("admin.workflows.list", OWNER, "Workflows.list", ADMIN_ERRORS,
              "GET /v1/admin/workflows", None, None),
    Operation("admin.workflows.create", OWNER, "Workflows.create", ADMIN_ERRORS,
              "POST /v1/admin/workflows", None, None),
    Operation("admin.workflows.get", OWNER, "Workflows.get", ADMIN_ERRORS,
              "GET /v1/admin/workflows/{workflow_id}", None, None),
    Operation("admin.workflows.update", OWNER, "Workflows.update", ADMIN_ERRORS,
              "PUT /v1/admin/workflows/{workflow_id}", None, None),
    Operation("admin.workflows.enable", OWNER, "Workflows.set_enabled", ADMIN_ERRORS,
              "POST /v1/admin/workflows/{workflow_id}/enable", None, None),
    Operation("admin.workflows.disable", OWNER, "Workflows.set_enabled", ADMIN_ERRORS,
              "POST /v1/admin/workflows/{workflow_id}/disable", None, None),
    Operation("admin.workflows.run", OWNER, "Workflows.run",
              (*ADMIN_ERRORS, "workflow_not_found", "workflow_disabled", "partial_failure"),
              "POST /v1/admin/workflows/{workflow_id}/run", None, None),
    Operation("admin.workflows.runs", OWNER, "Workflows.runs",
              (*ADMIN_ERRORS, *PAGINATION_ERRORS),
              "GET /v1/admin/workflows/{workflow_id}/runs", None, None, pagination=True),
    Operation("admin.workflows.run_get", OWNER, "Workflows.get_run", ADMIN_ERRORS,
              "GET /v1/admin/workflow-runs/{run_id}", None, None),
    Operation("admin.workflows.recover", OWNER, "Workflows.recover_interrupted", ADMIN_ERRORS,
              "POST /v1/admin/workflows/recover", None, None),
    Operation("admin.ai.providers", OWNER, "AIService.providers", ADMIN_ERRORS,
              "GET /v1/admin/ai/providers", None, None),
    Operation("admin.plugins.list", OWNER, "PluginRegistry.list", ADMIN_ERRORS,
              "GET /v1/admin/plugins", None, None),
    Operation("admin.plugins.install", OWNER, "PluginRegistry.install", ADMIN_ERRORS,
              "POST /v1/admin/plugins", None, None),
    Operation("admin.plugins.get", OWNER, "PluginRegistry.get", ADMIN_ERRORS,
              "GET /v1/admin/plugins/{plugin_id}", None, None),
    Operation("admin.plugins.grant", OWNER, "PluginRegistry.grant", ADMIN_ERRORS,
              "POST /v1/admin/plugins/{plugin_id}/grant", None, None),
    Operation("admin.plugins.enable", OWNER, "PluginRegistry.enable", ADMIN_ERRORS,
              "POST /v1/admin/plugins/{plugin_id}/enable", None, None),
    Operation("admin.plugins.disable", OWNER, "PluginRegistry.disable", ADMIN_ERRORS,
              "POST /v1/admin/plugins/{plugin_id}/disable", None, None),
)

# Routes that are deliberately outside the operation registry: static assets and
# the streamable-HTTP MCP mount. Both are asserted in tests as the only exemptions.
STATIC_ROUTES = frozenset({"/", "/admin", "/mcp", "/static"})

# Interface names used by compatibility reporting.
INTERFACES = ("http", "mcp", "cli", "core")
# Core actions are all bound through the single execute operation.
CORE_ACTIONS = (
    "list_chats",
    "search",
    "history",
    "get_message",
    "get_media",
    "send",
    "send_media",
    "edit",
    "delete",
    "forward",
    "set_title",
    "set_member_status",
)
# Mutating core actions must require an idempotency key on every interface.
MUTATING_ACTIONS = frozenset(
    {"send", "send_media", "edit", "delete", "forward", "set_title", "set_member_status"}
)
EXECUTE_PATH = "POST /v1/execute"


def by_name() -> dict[str, Operation]:
    return {operation.name: operation for operation in OPERATIONS}


def http_routes() -> dict[str, Operation]:
    return {op.http: op for op in OPERATIONS if op.http}


def mcp_tools() -> dict[str, Operation]:
    return {op.mcp_tool: op for op in OPERATIONS if op.mcp_tool}


def cli_commands() -> dict[str, Operation]:
    return {op.cli: op for op in OPERATIONS if op.cli}


def compatibility_report() -> dict[str, Any]:
    rows = []
    for operation in OPERATIONS:
        rows.append(
            {
                **operation.as_dict(),
                "interfaces": [
                    name
                    for name, value in (
                        ("http", operation.http),
                        ("mcp", operation.mcp_tool),
                        ("cli", operation.cli),
                    )
                    if value
                ],
            }
        )
    return {
        "schema_version": 1,
        "operations": rows,
        "core_actions": list(CORE_ACTIONS),
        "mutating_actions": sorted(MUTATING_ACTIONS),
        "notes": (
            "Every row executes through Gateway Core and the Policy Engine. The generic "
            "CLI 'request' command can reach any /v1 route and is intentionally outside "
            "the semantic parity guarantees; it cannot bypass Core or policy."
        ),
    }
