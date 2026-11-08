# Operations: synthetic-only development

**Synthetic data only.** The [deployment guide](DEPLOYMENT.md) is the authoritative Docker setup, including file ownership and explicit migrations. Hosted container smoke passed at the commit recorded in [status](STATUS.md); it does not verify this sandbox, a VPS, production, or real Telegram. Production, external exposure and real credentials require separate approval and the unresolved compliance decision.

## Local Docker setup

Follow [deployment configuration and startup](DEPLOYMENT.md#3-configure) on a disposable host. Start exactly one gateway process/worker/instance; do not use real Telegram data.

The application uses a **cooperative single-instance guard**: PostgreSQL holds a dedicated session-level advisory lock for this metadata database; a second cooperating gateway cannot start while that lock is held. Connect directly to PostgreSQL or through a connection pooler that preserves one backend session for the entire connection; transaction-pooling proxies are unsupported. Development SQLite uses a private `DATABASE_FILENAME.tg-control.lock` alongside the database, and requires POSIX locking and a private local directory. Do not delete or replace that lock file while the gateway is running; it remains after normal shutdown. SQLite URI options and shared file-URI databases are not supported by this guard, and different hard-link paths can bypass it. In-memory SQLite databases are isolated per application.

If a guard is lost, subsequent routed HTTP/MCP requests fail with `503 instance_guard_lost` and scheduled maintenance is skipped on its next check; audit storage failure can instead prevent the request from reaching the guard. The failed worker does not reacquire its lock: stop it, verify that no other writer/instance is running, reconcile potentially in-flight mutations and restart only after resolving the cause. This is **not distributed fencing**: noncooperating processes, database failover or lock loss between a check and an in-flight effect can still cause concurrent work; earlier requests and maintenance may finish. Backups still require all writers stopped. Treat the guard as a safety aid, not permission to run multiple instances.

While the gateway is running, a background task checks every enabled synthetic account at startup and 60 seconds after each completed pass, at most eight concurrently. Each cooperative asynchronous status call uses the operation timeout; an unresponsive event loop, database, or adapter that ignores cancellation can still delay the whole process. State-change events (including recovery, timeout and owner-intervention states) are stored in audit with account UUID and normalized codes only; the monitor does not request, return, interpret or act on message content, and no automatic reconnect is performed, and unchanged states are deduplicated across restarts. Initial healthy states do not produce alerts. Disabled accounts are removed from the stored monitor snapshot; re-enabling starts a new observation. The authenticated panel separately polls current status and filtered recent events while open. There is no push/email/SMS notification, monitoring while the gateway is offline, or guaranteed observation of a transient change between polls. The owner-only `GET /v1/admin/session-monitor` endpoint and Overview report task liveness, last pass outcome, last success age and current-pass duration; the panel warns if the task has stopped or its last pass failed. This process-local information resets at restart, and a completed pass does not imply sessions are healthy. There is no automatic stale threshold: investigate unexpectedly long passes, missing successes and the generic `session monitoring failed` log, along with audit history and current status. Health/readiness do not attest monitor progress; the service cannot report while offline or if its event loop is blocked. Logs are intentionally sanitized. The simulator persists each account as one encrypted document, so loading its status may decrypt/load that document including synthetic messages, although the monitor does not inspect them. No real adapter exists. For any approved remote design, TLS termination, a restricted ingress, exact allowed Host/Origin values and a separate security review are mandatory; static MCP Bearer credentials do not implement OAuth.

## Encrypted backup

Backups are **not automated**. Schedule them only with owner approval, keep encrypted copies off-host and separately protect both keys. The backup key is a second independent 32-byte URL-safe Base64/Fernet-format key in a 0600 file. The original gateway `encryption_key` is **not included** in the archive and must also be backed up securely, separately. Losing either key makes recovery impossible. The archive contains a PostgreSQL custom dump, policies, audit and optional encrypted per-account synthetic state. It does not contain the owner credential file; preserve that separately as needed.

Stop the gateway and any other writer first; the tool rejects a held synthetic state lock but cannot detect every possible database writer. Use compatible `pg_dump`/`pg_restore` binaries at least as new as the PostgreSQL server. The tool uses a 0600 temporary libpq service file rather than passing credentials in command arguments. PostgreSQL tools and the restore-target probe run with a minimal environment; ambient `PG*`, Python/loader settings and the caller's home credentials are ignored. Put required connection and TLS certificate paths explicitly in the private URL file, and use trusted client binaries that do not require `LD_LIBRARY_PATH`. It writes **plaintext temporary dumps** during backup and plaintext archives/dumps during restore; select a sufficiently large private tmpfs as `--scratch-dir` (for example `/dev/shm` on Linux), protect swap/core dumps, and inspect cleanup after interruptions. Filesystem snapshots and backups can retain expired/deleted content; manage their lifecycle separately. Do not store archives in the repository.

For the provided Compose setup, the optional `compose.backup.yaml` binds the DB to `127.0.0.1:55432` **only** while that override is used. After stopping the gateway (`docker compose stop gateway`), run `docker compose -f compose.yaml -f compose.backup.yaml up -d --wait db` to make the local DB available, and use the generated private `secrets/backup_database_url`, which points to `127.0.0.1:55432` with the same DB credentials; keep it outside Git. Run the installed host tool, pointing `--pg-bin-dir` at compatible PostgreSQL client binaries if necessary:

```sh
.venv/bin/tg-control-backup create --database-url-file ./secrets/backup_database_url \
  --backup-key-file ./secrets/backup_key --state-dir ./state/synthetic \
  --scratch-dir /dev/shm --output ./backups/synthetic-001.tgcbak
```

Existing archives are never overwritten. A failure after output creation can leave an incomplete encrypted file; it will not authenticate on restore and is deliberately left for owner inspection rather than automatically unlinked. If the simulator was ephemeral, omit `--state-dir` (only metadata is saved). Disable the backup port afterwards by recreating the DB service with `docker compose up -d --wait db`, then restart the gateway with `docker compose up -d gateway`; make no changes to external ingress. The DB service volume must remain intact; **never use** `docker compose down -v` as a routine operation.

## Restore and recovery

Restoring changes data and requires explicit owner approval. Test only against newly created disposable targets until approved. Create a **new, empty** PostgreSQL database and a private connection file for it; choose a **nonexistent** state directory. Stop the gateway and all writers. Confirm the intended target, then:

```sh
.venv/bin/tg-control-backup restore --database-url-file /private/new-empty-db-url \
  --backup-key-file /private/backup-key --input /private/backups/synthetic-001.tgcbak \
  --state-dir /private/new-synthetic-state --scratch-dir /dev/shm --confirm-empty-target
```

Omit `--state-dir` only for an archive without synthetic state. Authentication is verified before archive parsing, archive paths are restricted, an existing state directory and many detectable non-empty database objects are rejected, and `pg_restore` uses a single transaction. The catalog probe and restore use the same isolated libpq service; if the target or its catalogs cannot be inspected, restore fails closed. The restore role needs permission to inspect the relevant catalogs (including subscriptions). The probe is defense in depth, not proof that every possible object is absent or that another writer cannot change the database after checking: the owner must use a newly created database, stop all writers, isolate it during restore and explicitly confirm the target. Database and state restoration together are **not atomic**: a filesystem failure after database restore can leave partial targets. Do not overwrite or automatically delete them; inspect/retry only with owner approval. Restore the original gateway encryption key and point the gateway at the new DB/state; validate credentials, policies, audit and media using synthetic data. Backups may be older than current credentials or policies: revoke/rotate credentials and reconcile operations as appropriate. Test restore regularly into isolated targets.

## Migrations and troubleshooting

Back up and test migrations on an isolated copy before upgrading. Revision `0004` adds an audit event/id index without changing existing audit rows; index creation can still take time and locks on a populated database. Use the exact checked-out code version and `TG_DATABASE_URL_FILE=/private/url python -m alembic upgrade head` from the repository. Downgrades may lose security metadata and are not a substitute for backup; do not run them without explicit approval. If readiness fails, check DB reachability and migration revision, state-directory ownership/mode and exclusive lock, secret file access, and disk capacity. If media maintenance fails, stale bytes may remain until the next successful sweep; resolve the error and verify retention. A mutation with a lost response or audit failure may have occurred: reconcile rather than blindly retrying.

## Graceful shutdown and recovery

The lifespan shutdown is ordered and conservative:

1. The job registry stops accepting new claims (`jobs.close()`); the scheduler, event ingest, media maintenance and session monitor tasks are cancelled.
2. A cancelled mutating job handler is finalized as `outcome_unknown` with error `interrupted`; it is never retried automatically. A cancelled read-only handler is recorded as `cancelled`. If cancellation happens before the handler starts, the existing lease-expiry recovery resolves the claim at the next start.
3. Workflow runs still marked `running` are resolved to `outcome_unknown` at shutdown (the same recovery that runs at startup), so no run can be silently resumed.
4. Adapters are closed, the cooperative instance lock is released, and the SQLAlchemy engine is disposed (connections returned/closed).

In-flight HTTP requests are drained by the ASGI server before the lifespan shutdown runs; keep the server's graceful-shutdown timeout longer than the slowest documented operation. `SIGKILL`, a crash or a power loss bypass shutdown: jobs keep their leases until expiry and are then recovered as `outcome_unknown` (mutations) or retried only when the failure proved no effect (read-only), and workflow runs are recovered at the next start. Restarting the process is therefore safe with respect to duplicate execution, but an effect from an interrupted request must still be reconciled rather than repeated.

## Operator diagnostics

`GET /v1/admin/diagnostics` (owner) and `tg-control diagnose` print the same aggregate: unhealthy services, database reachability, failed jobs, stuck workflows, quarantined plugins, pending retries, interrupted/unknown operations, migration consistency, media-storage state and adapter failures. The report is read-only and aggregated: it contains no account IDs, tokens, DSNs, file paths or message content. Use `GET /v1/admin/traces`, `/v1/admin/metrics` and the JSON logs to correlate a specific `request_id`.

Triage order: readiness (`/health/ready`) → diagnostics `findings` → failed/stuck work by state → audit rows for the affected `request_id` → traces for the same id. Never paste tokens, session files or message content into a ticket.

## Metrics and log fields

Prometheus metrics exposed at `GET /v1/admin/metrics`:

| Metric | Labels | Meaning |
|---|---|---|
| `tg_control_http_requests_total` | `method`, `route`, `status_class`, `outcome` | Requests by transport method, route template and status class |
| `tg_control_http_request_duration_seconds` | `method`, `route` | Latency histogram (exposed as `_count`/`_sum`) |
| `tg_control_http_errors_total` | `route`, `status_class`, `outcome` | Server-side (`5xx`) responses |
| `tg_control_component_outcomes_total` | `component`, `kind`, `outcome` | Job, workflow, plugin and AI outcomes (including `outcome_unknown`, `cancelled`, `retried`) |
| `tg_control_metric_series_dropped` | — | Series dropped after the cardinality cap |

The label vocabulary is closed (`method`, `route`, `status_class`, `outcome`, `kind`, `state`, `component`); unknown labels are rejected and unknown outcomes collapse to `error`, so no account, client, chat or token value can become a label. `route` is always the route template (for example `/v1/admin/jobs/{job_id}`), never a raw path.

Logs are newline-delimited JSON written to stderr with a configurable level (`TG_LOG_LEVEL`). Fields are a fixed allowlist; values are truncated and sensitive keys (`text`, `message`, `content`, `body`, `caption`, `password`, `token`, `secret`, `authorization`, `phone`, …) are redacted. Record size is bounded. Never enable request-body or authorization logging.

## Backup verification

Every archive records manifest **version 2**: `includes_state`, `app_version`, `schema_revision` (the source Alembic revision) and `created_at`. Verify an archive without a database or a restore target:

```sh
.venv/bin/tg-control-backup verify --input /private/backups/synthetic-001.tgcbak \
  --backup-key-file /private/backup-key --scratch-dir /dev/shm
```

`--json` prints a machine-readable report (`format`, `manifest_version`, `app_version`, `schema_revision`, `created_at`, `includes_state`, `state_accounts`, `dump_bytes`, `archive_bytes`, `integrity`, `restore_state_dir_required`, `compatible`, `warnings`). Verification authenticates the ciphertext, validates the archive layout and canonical state paths, requires a non-empty dump, and never writes to the restore targets (it uses a disposable scratch directory). Add `--expected-schema-revision <revision>` to compare the recorded revision with the running application; a mismatch sets `compatible: false` and the command exits `3` (a legacy v1 archive without metadata produces a warning and skips the comparison). A v1 archive restores but records no revision.

## Retention pruning

| Data | Default | Trigger | Notes |
|---|---|---|---|
| Audit events | 90 days | `POST /v1/admin/maintenance/prune-audit` | Owner-only, bounded, audited |
| Event payloads | 30 days | `POST .../prune-events` | Subscription cursors are preserved |
| Synthetic media bytes | 30 days (`null` = never) | Access, startup, hourly sweep | Metadata remains |
| Terminal jobs + attempt history | operator choice (1–36500 days) | `POST .../prune-jobs` | Dry-run first; non-terminal work is never deleted |
| Finished workflow runs | operator choice (1–36500 days) | `POST .../prune-workflows` | `running` runs are never deleted |
| Cache entries | TTL (`0` disables) | Every write/invalidate | Process memory only |
| Logs | host policy | Log rotation / journald | Outside the database; not pruned by the gateway |

Every prune endpoint accepts `dry_run: true`, reports `eligible`/`deleted`, is idempotent (a repeat deletes nothing) and records an audit event. Pruning is deletion from the active store only: filesystem snapshots, backups and WAL can retain older bytes. Optional internal scheduled pruning is described below; external scheduling remains a deployment decision.

## Troubleshooting

| Symptom | Check | Action |
|---|---|---|
| `503 storage_unavailable` | Database reachability, disk, pool exhaustion | Retry read-only work; reconcile mutations (`outcome_unknown: true`) |
| `503 instance_guard_lost` | Another writer or a stale lock | Stop the process, confirm no other writer, reconcile in-flight work, restart |
| `503 migration_not_current` / readiness `migration` | `alembic_version` vs code | Run the pinned `alembic upgrade head`; never auto-upgrade |
| Readiness `scheduler` down | Diagnostics worker state | Restart; jobs are lease-recovered, not duplicated |
| `429 rate_limit` | `Retry-After`, audit `authentication` denials | Wait; rotate a leaked credential instead of retrying |
| `422 invalid_cursor` | Client mixing cursors across scopes/endpoints | Restart pagination from the first page |
| Stuck workflow run | `running` after a crash | Resolved to `outcome_unknown` at next start; reconcile the effect |
| Archive fails to verify | Wrong key file, truncated copy, revision mismatch | Restore from a verified copy; never "repair" ciphertext |

## Retention and automatic pruning

Three paths exist and they never conflict:

1. **Owner-triggered endpoints** (unchanged, default): `POST /v1/admin/maintenance/prune-audit`, `.../prune-events`, `.../prune-jobs`, `.../prune-workflows`. Jobs/workflow pruning takes `{older_than_days, dry_run}` and is dry-run-first.
2. **Media expiry** on the adapter path (30 days by default, `null` disables) — unchanged.
3. **Optional automatic pruning** (`auto_prune: true` in the stored retention settings; default **false**): the maintenance loop runs one bounded pass per `auto_prune_interval_minutes` (60–10080, default 1440) using the configured `audit_retention_days`, `event_retention_days` and `history_retention_days`.

Safety properties of the automatic path:

- It is an alias for the same routines: a queued, scheduled, running, retry-waiting or paused job and a running workflow run are never eligible.
- It checks the cooperative single-instance guard before running; noncooperating writers and loss after the check can still race.
- A fresh process waits a full interval before its first pass, so a short-lived process never deletes anything.
- One failing step does not stop the others; the pass is recorded as a failure and reported in diagnostics rather than retried in a hot loop.
- Each pass attempts one metadata-only audit row (`retention_auto_prune`, `decision: allow|deny`, `error_code: retention_failed`). If writing or committing it fails, diagnostics report failure; the missing record cannot be recovered from that diagnostic. Counts and identifiers are never written to audit.
- `GET /v1/admin/diagnostics` → `retention.auto_prune` reports `state` (`disabled|enabled|invalid`), `interval_minutes`, `last_run_age_seconds` and `last_run_outcome`.
- Disabling the setting stops future passes immediately; already-deleted history cannot be restored from the repository (restore from a verified backup instead).

```sh
# enable a daily pass keeping 90 days of history
curl -fsS -X PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"auto_prune": true, "auto_prune_interval_minutes": 1440, "history_retention_days": 90}' \
  http://127.0.0.1:8000/v1/admin/settings
```

## Deployment lifecycle (single instance)

Startup order is explicit and fail-closed: (1) the one-shot `migrate` service brings the schema to `head`; (2) `db` becomes healthy via `pg_isready`; (3) `gateway` starts and checks `alembic_version` plus the expected table set before binding traffic; (4) readiness becomes `200`, which is what the container health check uses. Any deviation (revision `behind`/`ahead`/`unknown`, tables missing, database unreachable) refuses startup with an actionable message and a `startup.schema_rejected` critical log line. The application never migrates itself, so an operator can always tell whether a schema change was intentional.

Stop order: `docker compose stop gateway` sends SIGTERM; uvicorn stops accepting, drains in-flight requests, runs the lifespan shutdown (scheduler, workflow recovery, engine disposal) within its 30 s window; Compose allows 45 s before SIGKILL. `docker compose down` keeps `./state`. The instance guard (PostgreSQL advisory lock) is released by the closing connection, so a restarted gateway reacquires it.

Resource bounds applied by Compose: gateway `mem_limit 512m`, `cpus 1.0`, `pids_limit 256`, read-only rootfs with tmpfs `/tmp` (64 MiB) and media staging (256 MiB), `cap_drop: ALL`, `no-new-privileges`, `init: true`; database `mem_limit 1g`, `pids_limit 512`, `shm_size 256m`; both cap `json-file` logs at 10 MiB × 3. See `DEPLOYMENT.md` for the full config/volume map and the blocked container-runtime checks.
