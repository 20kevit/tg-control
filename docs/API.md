# API contract

The intended HTTP API authenticates each request with a per-client Bearer credential and routes all actions through the Gateway Core. Every action names an account. An owner-only management API controls accounts, clients, scopes, retention and audit; owner credentials are never accepted as client credentials. All errors are sanitized and do not include secrets or content. Batch results are itemized; authorization of all targets occurs before any mutation. Discovery and search filter chats by Read scope, including a delivery-time recheck after in-flight reads and later batch actions. A policy change cannot retroactively cancel an already completed response or an external effect. The implemented routes are listed below; authenticated `GET /openapi.json` contains the machine-readable action schema. See `STATUS.md` for limitations.

## Synthetic gateway core contract

A validated batch has `actions` and, when any action mutates state, a client-generated UUID `idempotency_key`. The key is reserved durably before effects; reuse by the same client is rejected rather than replayed. Never retry an unknown result with a new key until reconciled. All targets are pre-authorized; a single denial prevents the entire batch from running. Once execution begins, outcomes are per item and are not atomic.

The base synthetic operations are `list_chats`, `search` (account-wide or within an explicitly selected chat), `history`, `get_message`, `get_media`, `send`, `edit`, `delete`, `forward`, `send_media`, `set_title`, and `set_member_status`. A further 135 typed feature actions share the same `Batch` and Gateway Core contract; 124 have synthetic adapter handlers, while unsupported, eligibility-gated, compliance-blocked, and approval-required rows remain explicit. Each feature action uses `operation` plus a strict, operation-specific `params` object. Each action explicitly contains `account_id`; chat-scoped actions also contain `chat_id`. Account-level actions do not accept a caller-provided chat ID. Forward additionally names `source_chat_id` and requires source Read plus destination Write in the same account. History pages are at most 100 (the documented TDLib page maximum), and report that history is not complete. A JSON request-size limit and 1 MiB synthetic media limit are resource-exhaustion protections, not Telegram product limits.

A feature action uses a discriminated `operation` and strict `params` model, for example:

```json
{
  "actions": [
    {
      "account_id": "<account-uuid>",
      "operation": "get_chat_info",
      "chat_id": "public-channel",
      "params": {}
    }
  ]
}
```

See [FEATURE_CAPABILITIES.md](FEATURE_CAPABILITIES.md) for the groups, status vocabulary and safety boundary; [the generated catalogue](contracts/capability_catalog.json) publishes all 147 entries, and [FEATURE_TRACEABILITY.md](FEATURE_TRACEABILITY.md) maps all 135 feature actions to requirements, handlers and tests. Client capability discovery is permission- and accessible-chat-filtered (including additional permissions such as Read + Write for `forward`); unavailable chat metadata hides chat-scoped rows. The owner-only endpoint retains the full catalogue. Synthetic fixture support is not live Telegram capability.

A 200 batch response does not imply every item succeeded. The CLI and MCP check that each requested action has a correlated, well-formed item outcome; the CLI exits nonzero and MCP marks the tool result as an error if any item failed. Valid partial results are retained. An invalid/missing mutation outcome must be reconciled, not blindly retried.

Rates: the adapter must only report `RateLimited` or `SessionUnavailable` when the operation was definitely rejected, with a consistent code and explicit false unknown-outcome flag. The core validates these fields and finite, non-negative retry durations again at the boundary; malformed or contradictory errors are never retried and in-flight mutations are conservatively reported as outcome-unknown. Generic `adapter_failure` on a mutation is also outcome-unknown. Valid rate-limit rejections wait the indicated duration within the deadline and recheck permissions/credentials; positive-duration rejections have no fixed retry-count cap while time remains. Only consecutive zero-duration rejections are capped at eight to prevent a tight loop. Retry waits and reauthorization are deadline-bounded. A timed-out in-flight mutation has an unknown outcome and is not automatically retried. The adapter can reconnect a disconnected synthetic session once, and the original operation is retried only if the returned session is `ready`; a reauthentication-required state is surfaced to the owner. Owner status and reconnect calls are deadline-bounded; a timed-out/failed reconnect reports `outcome_unknown` because the connection may already have changed. These checks cannot protect against an adapter that falsely claims a mutation was definitely rejected: the adapter must implement truthful outcome classification.

## HTTP routes

- `GET /health/live`, `GET /health/ready`: owner credential required; content-free health status. Liveness also depends on writing the mandatory request-start audit record; readiness additionally checks database connectivity. Both fail closed when audit storage is unavailable.
- `GET /openapi.json`: any currently active owner or client credential; no client-supplied role claims are trusted.
- `GET /v1/accounts`: client credential; returns only assigned accounts with at least one enabled permission. This is metadata discovery, not a Telegram content operation.
- `GET /v1/accounts/{account_id}/capabilities`: client credential; returns descriptive capability/eligibility metadata only for operations permitted by the client's current independent Read/Write/Admin scopes and actual accessible chat metadata. Missing, invalid or failed chat metadata hides chat-scoped rows. No chat IDs/titles/content are returned; Core rechecks active credentials and current scopes after the metadata lookups. This endpoint never reconnects or grants permission.
- `GET /v1/admin/accounts/{account_id}/capabilities`: owner credential only; shows the full account capability catalogue in the Admin UI. This owner surface is not exposed as an MCP tool.
- `POST /v1/execute`: client credential; JSON `{"actions": [...], "idempotency_key": "<UUID>"}`. Each action has `account_id`, `operation` and the operation-specific fields described by OpenAPI. A mutation batch requires a fresh UUID. A preauthorization failure returns 403/503 with per-item decisions and **no** effects; an already-used key returns 409. Once execution begins HTTP 200 contains individual success/failure results.
- Owner-only: `GET/POST /v1/admin/accounts`, `PATCH /v1/admin/accounts/{account_id}`, `GET .../status`, `POST .../reconnect`; `GET/POST /v1/admin/clients`, `POST .../{client_id}/rotate`, `POST .../{client_id}/revoke`; `GET/PUT /v1/admin/clients/{client_id}/accounts/{account_id}/policy`; `GET /v1/admin/audit`; `GET /v1/admin/session-monitor`; `GET /v1/admin/storage-status`; `GET/PUT /v1/admin/settings`; `POST /v1/admin/maintenance/prune-audit`.

The synthetic application holds a cooperative single-instance metadata-database guard. After loss is detected, new routed requests return `503` with `instance_guard_lost` and `outcome_unknown: false` for that rejected request; if request-start auditing fails first, `audit_unavailable` is returned instead. Previously accepted requests or maintenance may already have run or remain in flight, so reconcile their outcomes separately. See `OPERATIONS.md` for the restart procedure and limitations; the guard is not distributed fencing.

Client and owner secrets are distinct Bearer credentials. New/rotated client credentials are returned once, are not recoverable, and must be stored securely outside the repository. The owner token is never accepted on client routes; a client's Telegram Admin grant cannot access owner routes. Owner account creation accepts only the reserved synthetic phone range `+12025550100`–`+12025550199`.

`GET /v1/admin/session-monitor` is owner-only and reports process-local task liveness, current-pass duration, last completed result, last success timestamp/age and consecutive failures. It contains no exception text, account IDs or content, and is reset on process restart. A successful pass only means the monitoring code completed, not that all sessions are healthy; inspect `session_transition` events and account status separately. A running task can still be stuck, so check pass duration. The ordinary health/readiness endpoints do not check monitor progress.

`GET /v1/admin/audit` accepts an optional exact `event` filter, `limit` (1–200, default 100) and an opaque `cursor` (the legacy `before_id` form is still accepted). For example, `?event=session_transition&limit=5` returns the most recent synthetic-session transitions, including normalized state in `decision`, account UUID and sanitized `error_code`; it requires the owner credential. Background checks persist transitions while the process is running, not while offline, and never reconnect or inspect messages. Health endpoints do not attest that the monitor is functioning.

Scopes use `enabled`, `allow_types`, `allow_chats`, `exclude_types`, and `exclude_chats`. Type-wide grants are restricted to public types; private chats require explicit IDs. Exclusions override both grants. Omitted scopes default to disabled. The API validates Origin (when present), explicit Host, request size on every HTTP method, unambiguous HTTP framing and inputs; error responses do not echo input. Each authenticated API, MCP, health and schema request and gateway decision is audited without bodies or secrets. Static owner-panel bootstrap assets are publicly readable and contain no private data; a future remote deployment must put its own approved authentication in front of those assets as well.

Retention configuration is persisted. Audit records older than the configured interval (default 90 days) are removed when the owner calls the prune endpoint, or — only if the owner enables `auto_prune` — by the bounded in-process schedule described below. The bounded process-local read cache uses the configured TTL (0 disables it); no media bytes are cached, and authorization is rechecked before cache access. Synthetic media defaults to 30 days: bytes are removed on access, startup, and approximately hourly, including for disabled accounts; metadata remains. `null` disables age-based media expiry. Set `TG_SYNTHETIC_STATE_DIR` to a private directory for encrypted synthetic state to survive restarts; without it all simulator messages/media are ephemeral even with expiry disabled. Changing to a shorter media retention can immediately and irreversibly expire existing bytes. Backups/filesystem snapshots may retain deleted data. No real Telegram operations are implemented.

## Events (durable, pull-based)

- `POST /v1/events/subscriptions` (client): `{account_id, name, event_types[], chat_ids[], max_inflight}`. Requires current Read on the account; every listed chat is resolved server-side and must be readable. Duplicate names return `409 duplicate_subscription`.
- `GET /v1/events/subscriptions`, `DELETE /v1/events/subscriptions/{id}`, `POST .../pause`, `POST .../resume`: client-scoped lifecycle. Deletion removes that subscription's cursor, attempts and dead letters.
- `POST /v1/events/subscriptions/{id}/pull` with `{limit}` (1–100): returns `{events[], cursor, has_more, earliest_cursor}`. Each event carries `event_id`, `sequence`, `event_type`, `schema_version`, `source`, account/aggregate/chat identifiers, `occurred_at`/`observed_at`, correlation/causation IDs and a schema-validated `payload`. Only adapter-sourced events with a readable chat are delivered; operational events are never exposed to clients. Delivery rechecks the credential and Read scope before returning content.
- `POST .../ack` `{sequence}`: advances the cursor monotonically; a lower or future sequence returns `422 invalid_cursor`. Sequence `0` is the earliest cursor.
- `POST .../nack` `{event_id, reason?}`: records a bounded attempt. At the configured threshold the event becomes a dead letter for that subscription and the cursor advances so one poison event cannot block the stream.
- Errors: `401 credential_revoked`, `403 denied`, `404 subscription_not_found`/`event_not_found`, `409 subscription_paused`/`cursor_conflict`, `410 cursor_expired` (body includes `earliest_cursor`), `422 invalid_cursor`/`invalid_request`, `429 backpressure` (body includes `retry_after`).
- Owner-only: `GET /v1/admin/events/dead-letters[?subscription_id=]`, `POST /v1/admin/events/dead-letters/{id}/replay` (clears the attempt state and rewinds that subscription's cursor), and `POST /v1/admin/maintenance/prune-events` using the configured `event_retention_days` (default 30).

Ordering is guaranteed only within the gateway's persisted sequence; delayed, duplicated or out-of-order upstream delivery is possible and consumers must deduplicate by `event_id`. Delivery is at-least-once, not exactly-once.

## Durable jobs

- `POST /v1/jobs` (client): `{kind, input, schedule?, idempotency_key?, deadline_seconds?, max_attempts?}`. `schedule` is `{kind: "once", run_at: <ISO-8601 with offset>}` or `{kind: "daily_local", timezone: <IANA>, local_time: "HH:MM", misfire_policy: "skip"|"run_once", misfire_grace_seconds}`. Creation revalidates the credential and, for `telegram.send`, the account/chat Write authorization; unauthorized or invalid requests create nothing. A reused `idempotency_key` returns `409 duplicate_request`.
- `GET /v1/jobs`, `GET /v1/jobs/{job_id}`, `POST .../pause`, `.../resume`, `.../cancel`: the creating client only; another client's job returns `404`. Terminal jobs return `409 job_terminal` on further lifecycle changes. Cancelling a running job records a request: the run reports `cancelled` only when the handler confirms no effect, otherwise the real (possibly `outcome_unknown`) result stands.
- Owner-only: `GET /v1/admin/jobs`, `GET /v1/admin/jobs/{job_id}`, `POST /v1/admin/jobs/{job_id}/cancel`, `POST /v1/admin/jobs/tick` (one manual scheduler pass), `POST /v1/admin/jobs/recover` (resolves expired leases: mutating kinds become `outcome_unknown`, read-only kinds may retry within their attempt budget).
- The scheduler runs in-process while the gateway is up, every 15 seconds, and startup resolves leases left by a previous process. Jobs execute only with the creating client's credential; revocation or policy changes before execution prevent the effect. Execution through Gateway Core reuses the job ID as the mutation idempotency key, so a repeated execution cannot silently send twice.

`GET/PUT /v1/admin/settings` now also carry `event_retention_days`, plus the optional automatic-pruning fields: `auto_prune` (boolean, default `false`), `auto_prune_interval_minutes` (60–10080, default 1440) and `history_retention_days` (1–36500, default 90; used for terminal job records and finished workflow runs when auto-pruning runs). The response is always the full persisted object, so a partial `PUT` keeps defaults for fields it omits.

## Commands and media

- `GET /v1/commands` (client): the registered command catalog with version, reply requirement and required permissions. It is documentation metadata; no command is triggered by message content.
- `POST /v1/commands/save` (client): `{account_id, chat_id, message_id, idempotency_key}`. Resolves the replied-to message in the selected account through Gateway Core, stages any media privately (signature/size/SHA-256 validated), then sends the text or caption plus media to that account's Saved Messages through Core. Requires current Read for the source chat and Write for the destination chat. The idempotency key makes repeated invocations a conflict rather than a second save.
- `POST /v1/admin/commands/save` (owner): same body plus `client_id`. The owner explicitly delegates to a named client, so that client's policy applies; this is not a policy bypass and returns `404 client_not_found` if the client is disabled or has no active credential.
- Outcomes: `200` with `{command, version, status, account_id, source_chat_id, source_message_id, destination_chat_id, has_media, message_id}`; `401 credential_revoked`; `403 denied` / `destination_denied`; `404 unresolved_reply` / `unavailable_source`; `409 duplicate_invocation`; `413` media size/quota; `415 unsupported_media`; `422 unsupported_content` / `invalid_filename` / `invalid_media` / `size_mismatch` / `checksum_mismatch`; `429 rate_limit` (with `retry_after`); `502` upstream failure; `503 outcome_unknown` (`outcome_unknown: true`), never reported as success; `504 timeout`.
- Staged media lives only in a private server-owned staging directory (`TG_MEDIA_STAGING_DIR`, otherwise a private ephemeral directory), expires after one hour by default and is cleaned up at startup and hourly. No API exposes staging paths or filenames from other accounts.

## Owner-configured workflows

All routes below require the owner credential; client credentials receive `401`. A workflow is created against exactly one named client, whose policy is rechecked for every step.

- `POST /v1/admin/workflows` (201): `{name, trigger: "manual", conditions[], actions[], client_id, stop_on_error, timeout_seconds, enabled}`. At most 10 actions, 8 conditions, 4096 characters per message, 1–120 s runtime. `trigger` values other than `manual`, unknown action kinds and unknown/disabled clients are rejected (`422`).
- `GET /v1/admin/workflows`, `GET /v1/admin/workflows/{id}`, `PUT /v1/admin/workflows/{id}` (creates a new immutable version; renaming or changing the principal is rejected), `POST .../enable`, `POST .../disable`.
- `POST /v1/admin/workflows/{id}/run` `{parameters, dry_run}`: executes the enabled workflow. Returns the run with per-step outcomes (`success`, `skipped`, `failure` with a normalized error, `would_execute` for dry runs) and one of `succeeded`, `partial_failure`, `failed`, `outcome_unknown`. A dry run performs authorization only and creates no effects. Steps are idempotent per run and step index, so a repeated step cannot send twice.
- `GET /v1/admin/workflows/{id}/runs`, `GET /v1/admin/workflow-runs/{run_id}`: bounded execution history with the immutable definition version used.
- `POST /v1/admin/workflows/recover`: resolves runs left `running` by a previous process to `outcome_unknown` (startup does this automatically).

No event or message trigger exists. Nothing runs merely because a message arrived or resembles a command.

## AI task contract (no approved utility)

- `GET /v1/ai/tasks` (client): catalog of registered tasks. The shipped application registers none, so this returns an empty list until the owner approves an exact utility.
- `POST /v1/ai/tasks/{task}/run` (client): `{input, provider, model?}`. Unknown task, provider or model are `404`/`422`; there is no fallback. `input` must contain only task-declared fields. Any Telegram reference (`chat_id`, `account_id`, `message_id`, `messages`, `media`, ...) or credential-like key is refused with `422 compliance_blocked`/`invalid_input` before a provider is called. Oversized input is `413 input_too_large`; budgets `422 budget_exceeded`; provider timeout `504 timeout`; transient, not-applied provider failure `502 provider_unavailable`; ambiguous or malformed output `502 provider_error`/`invalid_output`. Responses carry `task`, `version`, `provider`, `model`, validated `output` and `usage` (tokens, estimated cost in USD, latency, attempts).
- `GET /v1/admin/ai/providers` (owner): declared capabilities of configured providers.

No AI request may include message content under the current compliance gate; the endpoint and provider adapters are synthetic-only.

## Plugins

Owner-only (`401` for client credentials).

- `POST /v1/admin/plugins` (201): install a manifest `{plugin_id, display_name, publisher, version, sdk_range, entry_point, extension_points[], capabilities[], client_id, account_ids[], budgets, config, health_check}`. Validation rejects unknown/duplicate capabilities, `admin`-style escalation, inconsistent extension points, non-semver versions, incompatible SDK ranges, malformed ids/entry points, nested configuration and inline secrets (only `$secret:name` references are accepted). Reinstalling the same version is `409 duplicate_plugin`; a non-increasing version is `422 downgrade_rejected`.
- `GET /v1/admin/plugins`, `GET /v1/admin/plugins/{plugin_id}`: state (`installed`, `enabled`, `disabled`, `quarantined`), granted capabilities, consecutive failures and module binding.
- `POST /v1/admin/plugins/{plugin_id}/grant` `{capabilities}`: owner grants declared capabilities only (`422 capability_not_declared`).
- `POST .../enable`: requires a bound first-party module and every declared capability granted (`409 module_not_bound`, `403 capability_not_granted`). `POST .../disable` stops use immediately.

A plugin never receives raw credentials, never acts for a client other than its delegated one, and cannot reach Core outside the capability-checked runtime. Actions are timeout-bounded and rate-limited; repeated failures quarantine the plugin (`plugin_timeout` `504`, `plugin_rate_limited` `429`, `plugin_error` `502`).

## Error envelope and status codes

Every HTTP error is one JSON object with a stable code:

```json
{
  "error": "invalid_cursor",
  "status": 422,
  "request_id": "5b0c…",
  "retryable": false,
  "outcome_unknown": false
}
```

- `request_id` is the correlation id for that request (also echoed in `X-Request-ID`). It is safe to quote in a support report; it is not a credential.
- `retryable` is advisory and only true for codes where repeating the same request cannot duplicate an effect (`rate_limit`, `storage_unavailable`, `timeout`, `busy`, …).
- `outcome_unknown` is true when a mutation may have been applied and the response was lost or failed after the effect (for example `storage_unavailable`, `internal_error`, an interrupted job, or an adapter failure that cannot prove rejection). Reconcile such an operation from history/audit; never blindly retry it with a new key.
- `retry_after` appears on `429` responses with the remaining lockout in seconds. `Retry-After` is also set.
- Batch-level rejection errors may additionally carry `items`, a bounded list of `{index, operation, status, error}` per action. Item text is sanitized.
- Validation errors (`422 invalid_request`) never echo the supplied value. `404`, `409`, `403` and `5xx` codes are stable strings; SQL, file paths, upstream messages and stack traces are never returned.

Status mapping: `400` malformed framework-level request, `401` missing/invalid credential, `403` denied by policy, `404` unknown or invisible object, `409` conflict/duplicate request, `413`/`414` input too large, `422` invalid request or cursor, `429` throttled, `503` storage/instance/adapter unavailable, `500` unclassified failure (always `outcome_unknown: true`).

Repeated invalid credentials from one source are throttled in-process: after 20 failures within 60 seconds the source receives `429 rate_limit` with `Retry-After` for 60 seconds, evaluated before the credential is compared (fail closed). The key is a digest of the transport source address; credentials are never used as keys or logged.

## Cursor pagination

Owner list endpoints that can grow without bound use opaque cursors:

- `GET /v1/admin/jobs?limit=&cursor=`
- `GET /v1/admin/audit?limit=&cursor=` (legacy `before_id` is still accepted)
- `GET /v1/admin/workflows/{workflow_id}/runs?limit=&cursor=`
- `GET /v1/admin/events/dead-letters?limit=&cursor=`; the client event inbox uses its own monotonic sequence cursor documented in the Events section.

Response shape:

```json
{
  "items": [ … ],
  "page": {"limit": 25, "returned": 25, "has_more": true, "next_cursor": "opaque"}
}
```

The list key is endpoint-specific (`jobs`, `events`, `runs`, `dead_letters`, `affected`) and is documented in the authenticated OpenAPI schema. Semantics:

- Ordering is stable and total: `(created_at DESC, id DESC)` for job records, `(started_at DESC, id DESC)` for workflow runs, `(timestamp DESC, id DESC)` for audit. A cursor encodes the last item and a scope (endpoint, client/account filter, page size) plus an expiry.
- `limit` defaults to 25 and must be 1–100. A request outside that range is `422 invalid_request`. The audit endpoint additionally permits up to 200 with a default of 100.
- `next_cursor` is `null` when the page is the last one. An empty first page returns `items: []`, `has_more: false`.
- Cursors are authenticated and bound to the requesting client (and to the account or workflow filter that produced them). Reusing a cursor from another client, another account, another filter or another endpoint returns `422 invalid_cursor`; nothing from the other scope is disclosed.
- A tampered, truncated, non-canonical, re-encoded or expired cursor returns `422 invalid_cursor` or `422 expired_cursor`; both are non-retryable except by restarting from the first page.
- Pagination is keyset-based, not offset-based: inserting or deleting rows between pages cannot duplicate or skip an item that was not already returned. A row deleted after being returned simply does not appear again; a row modified between pages is returned once, in its position for the ordering key it had when read.
- Authorization is reapplied on every page: a revoked credential or removed account grant fails the continuation request even if the first page succeeded. Rate limits apply per request, not per cursor walk.

## Health, diagnostics, observability

- `GET /health/live` — owner credential required; process/event loop liveness only.
- `GET /health/ready` — owner credential; `database`, `migration`, `instance_guard`, `scheduler` states. Returns `503` with `ready: false` when any dependency is unavailable; no secret, DSN or path is included.
- `GET /health/dependencies` — owner credential; database state, scheduler worker state and media staging state.
- `GET /v1/admin/metrics` — owner credential; Prometheus text format with bounded label sets (`method`, `outcome`, `route` (template), `state`, `kind`) and no account, client, chat or token labels. A cardinality cap drops excess series and increments `tg_control_metric_series_dropped`.
- `GET /v1/admin/traces?limit=` — owner credential; recent in-process spans with name, component, duration, outcome and parent id (no payloads).
- `GET /v1/admin/diagnostics` — owner credential; read-only aggregate report (`database`, `jobs`, `workflows`, `events`, `plugins`, `accounts`, `retention`, `runtime`, `findings`) with `read_only: true`. Account identifiers are not included.

Structured JSON logs use `event`, `level`, `trace_id`, `request_id`, `workflow_id`, `job_id`, `component` and `outcome` fields. Message content, credentials, phone numbers, filenames and raw adapter text are redacted by key allowlist and value bounds.

## Owner maintenance

- `POST /v1/admin/maintenance/prune-audit`, `.../prune-events` — existing retention pruning; bounded deletion, audited.
- `POST /v1/admin/maintenance/prune-jobs` `{older_than_days, dry_run}` — deletes terminal job rows (and their attempt history) older than the window. Non-terminal states (queued, scheduled, retry_wait, running, paused) are never touched.
- `POST /v1/admin/maintenance/prune-workflows` `{older_than_days, dry_run}` — deletes finished workflow runs older than the window; `running` runs are never deleted.
- Both accept `older_than_days` 1–36500 and `dry_run` (strict boolean). A dry run reports `eligible` and never writes; an applied run reports `deleted` and adds an audit event. Repeating a pruned call returns `deleted: 0` (idempotent).
