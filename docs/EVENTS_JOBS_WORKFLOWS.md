# Events, durable jobs, and workflows contract

Status: required engineering foundation. Implementation remains synthetic-only while the compliance gate is unresolved. This contract defines reusable infrastructure, not permission to process real Telegram data.

## Implementation status (2026-11-07)

| Contract area | State | Evidence |
|---|---|---|
| 1.1 Envelope, schema versions, payload limits | Implemented | `src/tg_control/events.py` (`EventInput`, `EVENT_PAYLOAD_KEYS`); `tests/test_events.py::test_envelope_validation_rejects_malformed_and_oversized_input` |
| 1.2 Durable ingestion, dedup, retention | Implemented | `EventService.ingest`, unique `fingerprint`; `test_ingestion_is_durable_and_deduplicated`, `test_paused_subscription_and_pruned_cursor` |
| 1.3 Subscriptions, filters, cursors, ack, replay, dead letters, backpressure | Implemented (pull transport only) | `EventService.pull/ack/nack/replay_dead_letter`; `test_cursor_persistence_ack_monotonicity_and_conflict`, `test_nack_retries_then_dead_letters_with_controlled_replay`, `test_backpressure_isolates_a_slow_consumer` |
| 1.3 Authorization recheck before delivery; operational events owner-only | Implemented | `EventService._deliverable`, `test_pull_filters_and_rechecks_read_scope_before_delivery`, `test_operational_events_are_never_delivered_to_clients` |
| 1.4 Public event API and external transports (webhooks/SSE/WS) | Not implemented (deliberate) | HTTP pull/ack API exists; no push transport is shipped without a separate transport/security review |
| 2.1 Job record fields and bounded history | Implemented | `src/tg_control/db.py` `Job`/`JobRun`; migration `0006` |
| 2.2 State machine and terminal semantics | Implemented | `jobs.validate_transition`, `Jobs._finalize`; `tests/test_jobs.py::test_state_machine_rejects_invalid_transitions`, `test_unknown_outcome_is_terminal_and_never_retried` |
| 2.3 Scheduling, DST, misfire, pause/resume/cancel, leases, recovery, graceful shutdown | Implemented | `next_local_occurrence`, `skip_misfired`, `claim`, `recover_expired_leases`, `Jobs.close`; corresponding tests in `tests/test_jobs.py` |
| 2.3 Permission revalidation immediately before execution | Implemented for the registered `telegram.send` kind (Gateway Core recheck) | `src/tg_control/job_kinds.py`; `test_delayed_send_denied_when_policy_revoked_before_execution`, `test_delayed_send_blocked_when_credential_revoked` |
| 2.4 Definite-failure-only retry, idempotency, unknown-outcome protection | Implemented | `Jobs._finalize`, job-ID idempotency key in `job_kinds.py`; `test_retry_only_for_definite_failures`, `test_gateway_execution_key_prevents_duplicate_delayed_effect` |
| 3 Workflow engine | Not implemented | See `ROADMAP.md`; no workflow code or schema exists yet |

Limits: single-process scheduler; ordering/delivery are at-least-once; a client that never acks retains its backlog until event retention removes it (`cursor_expired` then reports the earliest resumable cursor); dead-letter replay is owner-driven; there is no external scheduler HA or webhook transport.

## 1. Event model

### 1.1 Normalized envelope

Every internal event uses a versioned envelope with:
- event_id: globally unique immutable identifier.
- schema_version and event_type: explicit schema and stable namespaced type.
- occurred_at: source event time when known; nullable if unavailable.
- observed_at: gateway UTC timestamp when received.
- account_id: gateway account UUID, never a caller-supplied path.
- aggregate_type and aggregate_id: changed entity, when applicable.
- source: adapter, core, command, scheduler, or approved plugin.
- correlation_id and optional causation_id.
- sequence: source sequence only when upstream guarantees it; otherwise omitted.
- payload: typed, schema-validated minimum fields needed by the consumer.

Never invent a global Telegram ordering guarantee. Preserve upstream ordering only where documented; otherwise state that events can be delayed, duplicated, or out of order. Consumers cannot depend on ordering across chats/accounts.

### 1.2 Ingestion and persistence

- Normalize adapter callbacks at the adapter boundary; raw TDLib objects never leak into public API contracts.
- Validate event type/version, account ownership, payload size, and correlation before persistence.
- Persist accepted events before acknowledging durable receipt. Use transactional inbox/outbox or equivalent so state changes and publication cannot silently diverge.
- Deduplicate using stable upstream identity where available; otherwise use a documented deterministic fingerprint with a bounded horizon. Never merge conflicting identities silently.
- Event retention is configurable and distinct from audit and media retention.
- Event payloads are untrusted data. No event executes a command, workflow, plugin, or AI task merely because its payload contains instruction-like text.

### 1.3 Subscription and delivery

- Subscriptions are explicit per client and account; default is none.
- Filters are declarative and cannot widen Read/Write/Admin grants.
- Before delivering message-derived content, re-check current Read permission for the client/account/chat. Redact or omit fields not covered by the grant.
- Metadata-only operational events are owner-only unless a separate operator scope is explicitly approved; never leak other clients' activity.
- Delivery is at-least-once, not exactly-once. Consumers deduplicate by event_id.
- Each consumer has a durable checkpoint/cursor. Acknowledge only after durable consumer acceptance.
- Support replay/resume within retention. Expired/invalid cursors return a typed error and earliest available cursor; never silently skip history.
- Retry transient failures with bounded exponential backoff and jitter. Poison events move to a dead-letter record after configured attempt/age thresholds, with sanitized reason and controlled replay.
- Pause/resume/revoke subscriptions. Revocation stops future delivery but cannot retract already delivered content.
- Isolate slow consumers with bounded queues and backpressure. One consumer cannot block Telegram update ingestion or unrelated clients.

### 1.4 Public event API

Before external event delivery, define and version:
- Subscription create/update/delete and credential ownership.
- Filter schema and supported event types.
- Cursor format, retention, replay/resume, expiration.
- Acknowledgement and retry semantics.
- Maximum in-flight deliveries and backpressure response.
- Error envelope and compatibility policy.
- Webhook signature, timestamp tolerance, replay protection, retry schedule, and secret rotation if webhooks are supported.

MCP subscriptions, Streamable HTTP, webhooks, SSE, or WebSocket are transports, not separate business logic. All use the same authorization and event service. Do not ship a transport until auth, Origin/Host, signature, and lifecycle behavior are specified.

## 2. Durable job and scheduler model

### 2.1 Job record

A durable job records:
- Stable job_id, schema version, creator principal, client/account scope, creation time.
- Typed job kind and versioned input reference; avoid copying message bodies into job metadata.
- run_at UTC instant and optional IANA timezone plus original local schedule expression.
- State, attempt count, next attempt, deadline, lease/claim metadata, cancellation request, timestamps.
- Idempotency identity and correlation ID.
- Last normalized result/error code and bounded execution history.

Secrets and raw credentials never appear in job payloads, logs, or results. Large media is stored separately and referenced by opaque IDs with retention enforcement.

### 2.2 State machine

Required states:

queued → scheduled → running → succeeded | failed | outcome_unknown | cancelled | expired

Additional transitions:
- running → retry_wait → scheduled only for a definitely-not-applied retryable failure.
- queued/scheduled/retry_wait → cancelled before execution.
- running → cancel_requested → cancelled only when the operation confirms no effect; if an external effect may have occurred, resolve to outcome_unknown.
- running → outcome_unknown on timeout, lost connection, ambiguous adapter response, or process crash around a mutation.
- outcome_unknown is terminal for automatic retry. Reconcile via authoritative status or owner action; never create a new idempotency key and resend blindly.

Persist and validate transitions transactionally. Terminal states are immutable except for an explicit reconciliation record preserving history.

### 2.3 Scheduling and execution

- Persist UTC instants and IANA timezone identifiers for recurring local schedules.
- Define daylight-saving behavior: default to skip nonexistent local times and execute repeated local times once unless an explicit schedule policy says otherwise.
- Define misfire policy per job: skip, run once immediately, or bounded catch-up. No unbounded catch-up storm.
- Persist schedules and next-run calculation so restart does not lose work. Recompute after timezone database changes and record the result.
- First implementation may use a single scheduler/worker consistent with the repository's one-instance constraint. Do not claim distributed scheduling or multi-worker safety.
- Claim work with bounded leases; recover stale claims only when safe to establish no external mutation occurred.
- Apply per-client, per-account, and per-operation concurrency controls. Telegram rate limits remain authoritative; no arbitrary product-level batch cap is introduced.
- Support pause, resume, cancel, inspect, and bounded execution history. Graceful shutdown stops new claims, persists checkpoints, and lets safe in-flight work finish within a deadline.
- Every delayed operation re-checks client credential, account assignment, chat scope, operation permission, exclusions, and owner policy immediately before the effect.
- Revocation or policy change prevents future execution; it cannot undo an already completed external effect.

### 2.4 Retry and idempotency

- Retry only explicit failures proving no effect was applied and providing a valid retry instruction.
- Respect the exact platform wait duration within the job deadline. Reauthorize after every wait.
- Do not retry timeouts, lost responses, malformed adapter errors, generic mutation failures, or unknown outcomes.
- Use stable idempotency keys scoped to principal and operation. Persist reservation/result before acknowledging completion.
- If the platform offers reconciliation/status, use it before deciding whether a prior uncertain operation may be retried.
- Record attempts separately; never overwrite an earlier unknown outcome without a reconciliation trail.

## 3. Workflow engine

### 3.1 Model

A workflow is a versioned, owner-configured definition consisting of:
- Explicit trigger: manual invocation, approved command, or explicitly enabled event type.
- Optional typed conditions over allowlisted metadata/fields.
- Ordered actions with typed input/output references.
- Per-step timeout, retry policy, and failure policy.
- Enabled/disabled state, creator, scope, version, and audit metadata.

No implicit trigger on arbitrary message arrival, text semantics, or inferred intent. Incoming message content is never an instruction. Any future user-defined content rule must be opt-in, explicit, narrowly scoped, documented, and tested; it cannot grant permission or bypass compliance.

### 3.2 Execution guarantees

- Validate definitions before activation; reject unknown actions, invalid references, unbounded loops, and incompatible schema versions.
- Execute through Gateway Core and the same Policy Engine; a workflow is not a privileged principal.
- Re-check permissions and account/chat authorization before every action, not only at trigger time.
- Bound step count, runtime, concurrency, payload size, and recursion depth. Reject cycles unless an explicit bounded-loop construct is approved.
- Persist execution state and per-step outcomes so restart resumes only safe steps. Unknown mutations are never replayed automatically.
- Define stop-on-error vs continue-on-error per workflow; return step-indexed outcomes and preserve successful prior effects.
- Support dry-run/preview where possible. Dry-run creates no external effects and identifies checks it cannot guarantee.
- Version definitions immutably per run; edits affect future runs only.
- Provide owner controls to disable workflows and inspect execution history.

### 3.3 Rule safety

- Conditions inspect only fields exposed by the event schema and permitted by policy.
- No eval, arbitrary Python/JavaScript, shell, SQL, URL fetch, or dynamic code execution in workflow definitions.
- Prevent self-trigger loops using causation IDs, trigger-origin checks, per-run depth, and rate limits.
- User-configured automation is opt-in and auditable. A content match is not authorization; Policy Engine remains authoritative.

## 4. Acceptance criteria

- Event tests: duplicate, delayed, out-of-order, malformed, oversized, expired cursor, replay, consumer crash, poison event, revoked subscription, slow consumer, cross-account isolation, and authorization change during delivery.
- Job tests: every state transition, restart recovery, timezone/DST, misfire, pause/resume/cancel, stale lease, revoked credentials, policy changes, rate limits, deadline, duplicate prevention, unknown mutation, and graceful shutdown.
- Workflow tests: invalid schema, disabled trigger, event spoofing, content-as-instruction, loop prevention, step timeout, partial failure, retry safety, per-step authorization, revocation, dry-run, restart recovery, and immutable run version.
- Synthetic fixtures only until the compliance gate is resolved. Public event delivery and external webhooks require separate transport/security review.
