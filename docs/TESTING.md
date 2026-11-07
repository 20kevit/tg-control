# Test strategy

Tests must use generated/synthetic data only. HTTP negative tests cover streamed GET/OPTIONS as well as mutation bodies, conflicting or duplicate framing headers and malformed lengths. Required evidence: policy matrix and exclusions; private-by-default and new-chat behavior; credentials/rotation/revocation; account isolation and IDOR; pre-authorization of every batch target including forwarding sources; per-item failure outcomes; known-rejected retry-after (including more than eight positive waits), bounded retry/reauthorization and unknown write outcome handling; bounded session failures; audit secrecy and retention; API, CLI, MCP, panel and end-to-end tests; PostgreSQL migrations and recovery where an isolated server is available. Test allow and deny paths for every interface. Tests may not be disabled or weakened to obtain a green run. Exact executed commands and unavailable dependencies are recorded in `STATUS.md`.

The suite includes an actual headless Chromium workflow for the owner panel (including stored-XSS negative checks and narrow viewport), official MCP SDK Streamable HTTP and stdio subprocess clients, CLI/API authorization tests, and temporary Unix-socket PostgreSQL integration. Install the Python and Node development dependencies in `DEVELOPMENT.md` before running the whole suite. No tests use live Telegram.

The backup/restore integration uses disposable PostgreSQL servers and verifies wrong keys, ciphertext tampering, non-empty targets (including operators, text-search objects, foreign wrappers, database settings and offline-created subscriptions), poisoned ambient libpq settings, incomplete archives and traversal/manifest rejection. A restricted-role catalog probe and inaccessible-target failure are covered. These checks do not prove a target is completely empty or safe against concurrent writers. An optional native TDJSON probe is skipped unless `TG_TDLIB_LIBRARY` points to a locally built library; on Linux the probe installs a seccomp filter denying network syscalls before loading the library, uses no parameters or credentials, and closes both empty clients. This feasibility test is not a live Telegram test.

Synthetic session-monitor tests cover persistence and deduplication across restarts, recovery, disabled-account races, per-account timeout and malformed status handling, transactional audit failures, no automatic reconnect, sanitized background failure/retry and shutdown cancellation. An 11-account case checks bounded concurrency without an account-count cap. Disposable PostgreSQL tests exercise transition persistence and additive migration from revision `0003`; browser tests cover owner-only history rendering and XSS-safe clearing. These checks do not prove behavior for a blocking/noncooperative native adapter, downtime or external alert delivery.

`test_adapter_error_boundary.py` deliberately mutates synthetic adapter exceptions: contradictory unknown-outcome flags after a simulated effect, invalid delay types/values, altered error codes and non-boolean status flags. It verifies no second effect, no retry/reconnect on inconsistent failures, conservative unknown results, sanitized Core/HTTP/MCP/audit outputs, delivery-time denial and a ready-only reconnect. The contract still relies on a truthful adapter and cannot prove native Telegram behavior.

`test_monitor_progress.py` verifies owner-only process-local progress, in-flight duration, sanitized failure and recovery, task cancellation, audit and reset behavior; browser tests check failure/stopped/malformed warnings, text-only rendering, out-of-order response races and clearing on lock. Progress is not durable, health endpoints do not attest it, and these tests do not simulate a blocked event loop or external notification.

Single-instance regression cases are in the existing API and PostgreSQL test files. They use disposable local databases to verify process-level SQLite exclusion, PostgreSQL advisory-lock contention and non-superuser checks, startup/shutdown cleanup, lock-file replacement, backend termination, latched fail-closed responses, sanitized errors and no effect from rejected new requests. These tests do not prove distributed fencing, behavior during failover, protection from noncooperating writers or cancellation of in-flight operations.

CLI and MCP regression tests cover fully successful, mixed-success/failure, malformed/missing/contradictory batch results and non-2xx responses that falsely resemble success. The suite exercises the real local CLI and MCP stdio subprocesses against the synthetic gateway, preserving per-item outcomes and conservatively marking unknown mutation responses without automatic retries.

## Durable events and jobs

`tests/test_events.py` exercises synthetic event ingestion against a temporary SQLite database: envelope/schema/payload validation, account and chat resolution, duplicate suppression by upstream identity (and persistence across a new service instance), subscription authorization and duplicate names, delivery filtering with a Read-scope change mid-stream, credential revocation, cursor monotonicity/conflict/ownership, at-least-once resume after restart, backpressure isolation between two subscriptions, attempt-bounded dead letters with owner replay, adapter update draining, account isolation, owner-only operational events, pause/resume, retention pruning with typed cursor expiry and subscription deletion. `tests/test_events_jobs_api.py` repeats the lifecycle over authenticated HTTP (including owner-only routes and cross-client isolation) and verifies event retention settings.

`tests/test_jobs.py` uses an injected clock so timezone, DST, misfire, retry and recovery behavior is deterministic: invalid transitions, spring-forward skip and fall-back single-run, schedule validation, enqueue authorization/duplicate keys/revoked credentials, due execution with persisted history, retry only for definite rate limits, terminal unknown outcomes, handler timeout for mutating kinds, pause/resume/cancel/deadline, cooperative cancellation without effect, expired-lease recovery (unknown for mutations, retry for read-only), skip/run-once misfire policies, recurring advancement, per-client job isolation, registry validation, scheduler tick and graceful close, plus Gateway integration proving execution-time policy/credential revalidation and single-effect idempotency. No test sleeps on wall-clock time or uses real Telegram data.

## Commands and media

`tests/test_media.py` stages synthetic bytes through a private temporary root: checksum/size verification and re-read verification, filename and MIME allowlist rejection (traversal, separators, control characters, leading dot/trailing dot, unsupported types, bogus base64), signature mismatch, declared-size and checksum mismatch, per-file/account quota enforcement, guarded staging denial, streaming cancellation leaving no partial files, TTL cleanup, and rejection of symlinked roots/account directories.

`tests/test_commands.py` exercises `/save` through the real Core/Policy/Media/Audit path with synthetic fixtures: text and media replies, caption and filename preservation, source Read and destination Write denial, unresolved reply, cross-account isolation, duplicate invocation via the caller's idempotency key, empty/unsupported content, expired source media, rate-limit reporting with `retry_after`, unknown-outcome reporting, revoked credentials, staging failure cleanup and metadata-only audit. `tests/test_commands_api.py` repeats the important cases over authenticated HTTP, including the owner-delegated route that applies a named client's policy.

## Workflows

`tests/test_workflows.py` validates definition rejection (empty/oversized action lists, over-long text, event triggers, unknown action kinds, invalid timeouts and malformed conditions), client existence, duplicate names, manual execution through Core, parameter-driven conditions and skipped steps, dry-run authorization without effects, stop-on-error versus continue-on-error with per-step outcomes, immutable version snapshots, disabled and unknown workflows, a mid-run policy change that denies the second step after the first effect, conservative interrupted-run recovery, revoked credentials before execution and bounded history. `tests/test_workflows_api.py` covers the owner-only HTTP surface, dry-run, execution reaching the synthetic adapter, denied steps after policy revocation, validation errors, disable/enable, version bumps and 404 handling.

## AI task contract

`tests/test_ai.py` proves explicit-only invocation (no provider call before `invoke`), exact input passthrough, rejection of Telegram references and credential-like keys with no provider call, unknown task/provider/model, capability mismatch without fallback, input and cost budgets, timeout, retry only for retryable not-applied errors (ambiguous and non-retryable errors are not retried), output-shape rejection, credential revocation, duplicate/illegal registrations (including event invocation), an empty default catalog, and audit rows containing no input or output. `tests/test_ai_api.py` proves the empty catalog, owner-only provider listing, 404 for unknown tasks, compliance rejection over HTTP when a conformance task is registered in-test, and credential requirements. The conformance task and `FakeProvider` are engineering fixtures, not product utilities.

## Plugins

`tests/test_plugins.py` validates manifest rejection (ids, entry points, versions, SDK range, `admin` capability, inline and nested configuration, missing delegated client, inconsistent extension points), install-without-grant behavior, module binding, grant bounds, principal mismatch, disabled-state refusal, delegated-client execution through Core including denial after policy revocation, rate limiting, timeout containment, quarantine after three failures with Core unaffected and no secret text in audit, and upgrade semantics (duplicate, downgrade, new version requiring re-grant and rebinding). `tests/test_plugins_api.py` covers the owner-only HTTP surface, install/grant/enable/disable, over-grant rejection, manifest validation and unknown-plugin 404.

## Offline TDLib boundary

`tests/test_tdlib_boundary.py` monkeypatches `socket.socket`/`socket.create_connection` to fail and runs authorization, update intake, update draining and paginated history to prove no network path exists. It also asserts `ComplianceBlocked` for live construction even with plausible enabling environment variables, rejects real-looking fixture markers, walks every credential-bearing authorization state to `reauth_required`, rejects unknown states, closes sessions terminally, verifies duplicate/unknown/unsafe update drops, sequence ordering, buffer bounding, pagination with `before_id`, `not_found` for unknown chats, refusal of every mutation operation and the absence of a Saved Messages resolution. All fixtures are synthetic; no native library, TDLib parameter, account or live authentication is involved.

## Traceability

`tests/test_traceability.py` keeps `docs/TRACEABILITY.md` honest: every referenced implementation path must exist, every listed `tests/test_*.py::test_name` must still exist, every test file must appear in the matrix, and the matrix must not contain live or production-readiness claims. Regenerate after adding, renaming or removing tests:

```
python scripts/gen_traceability.py
```

The matrix covers 17 requirement areas and the full executed-test inventory. It proves mapping and execution, not product correctness, and deliberately contains no rows for unimplemented areas (live TDLib adapter, real AI providers, remote plugin distribution, deployment).

## Interface contracts and CI gate

Run the interface gate exactly as CI would:

```sh
.venv/bin/python scripts/gen_contracts.py      # regenerate docs/contracts/interfaces.json
.venv/bin/python scripts/check_interfaces.py   # stale matrix, missing MCP/CLI bindings, owner-over-MCP
.venv/bin/pytest -q tests/test_contracts.py tests/test_mcp.py
```

`tests/test_contracts.py` binds the registry to the live application: every operation must appear in `app.routes` with the recorded method, every client operation must also be reachable through MCP and CLI, owner-plane operations must not be MCP tools, MCP argument models and the CLI catalog must exist, paginated operations must expose a cursor parameter in `app.openapi()`, error codes must normalize to the registry vocabulary, Core references must resolve, and `docs/contracts/interfaces.json` must be current. `tests/test_mcp.py` compares the live tool catalog against `mcp_server.TOOLS` instead of a hand-written list.

## Pagination

`tests/test_pagination.py` covers cursor determinism and scope binding, tamper and forged-key rejection, non-canonical base64, malformed and over-long tokens, expiry (`expired_cursor`), identifier validation, `normalise_limit` (defaults, booleans, zero and over-maximum), full job walks (first, intermediate, final, empty), cross-client and admin cursor reuse (`422 invalid_cursor`), concurrent inserts and deletes between pages, audit pagination with the legacy `before_id`, workflow-run pagination with a cross-workflow cursor, and metadata typing.

## Observability and health

`tests/test_observability.py` covers JSON log redaction and bounding, hostile values that must never raise, the metric label allowlist, outcome normalization, the series cap, span hierarchy/parent ids/redaction, `/health/live|ready|dependencies` (owner-only, no secrets), owner-only metrics/traces/diagnostics, diagnostics aggregates without account ids, request-id echo/regeneration, error envelopes that never echo input, and configurable log levels.

## Retention, throttling and shutdown

- `tests/test_retention_pruning.py` proves dry-run-first pruning of terminal jobs and finished workflow runs, preservation of non-terminal work, attempt-history cascade, audit events, idempotent repeats, owner-only access and bounded request validation.
- `tests/test_throttle.py` covers the failure window, lockout and recovery, bounded source tracking, digest-only keys, `429` + `Retry-After` over HTTP, fail-closed behavior during lockout, success reset and audit without the credential.
- `tests/test_jobs.py` includes a cancelled mutating execution that must be finalized `outcome_unknown`/`interrupted` and must not be retried by lease recovery.
- `tests/test_backup.py` covers manifest v2 round-trip, legacy v1 warnings, wrong key, tampered ciphertext, truncation, revision mismatch (`compatible: false`, exit `3`), invalid layout/empty dump, a read-only verify that writes nothing, and the `verify` CLI.

## Exact commands and current result

```sh
.venv/bin/pytest -q                                  # 391 passed, 1 optional native probe skipped
.venv/bin/ruff check src tests alembic scripts        # clean
.venv/bin/python scripts/check_interfaces.py          # 72 operations, 20 MCP tools, 26 CLI commands
.venv/bin/python scripts/gen_traceability.py          # matrix regenerated, 4 checks pass
```

The optional native TDLib probe is skipped unless `TG_TDLIB_LIBRARY` points at a locally built library. Docker/Compose runtime and remote OAuth interoperability remain unverified and are not claimed.
