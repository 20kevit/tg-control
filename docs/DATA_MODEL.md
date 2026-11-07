# Data model and storage

The target PostgreSQL metadata schema contains accounts (stable UUID, alias, encrypted phone metadata, state), clients, credential digest/rotation/revocation metadata, account assignments, independent permission scopes and explicit exclusions, settings, and audit decisions/outcomes. Raw bearer credentials are displayed once, never stored; a high-entropy credential can be verified by a cryptographic digest. Phone numbers and any future session encryption material need authenticated encryption with an independently mounted key.

A Telegram account's TDLib database/files must reside in a dedicated UUID-derived directory and use a distinct encryption key; clients must never supply filesystem paths. No actual Telegram state is present during development. Schema migrations must be versioned; migration/rollback and recovery instructions belong in the operations guide. Audit retention defaults to 90 days and is configurable; media retention may be temporary or explicitly permanent. Any current implementation limitations are listed in `STATUS.md`.

## Implemented synthetic state and cache

`TG_SYNTHETIC_STATE_DIR` optionally enables isolated per-account synthetic state files. The directory and account directories must not be group/world accessible; filenames are generated from server-owned UUIDs. Each account's authenticated-encryption key is derived from the mounted master key with HKDF and the account UUID; files are written with restrictive permissions and atomic replacement. A POSIX lock rejects a second process using the same directory. This is simulator storage, **not** TDLib state. Without the directory, source state is lost on restart. Protect and back up the master key separately; losing it makes encrypted phone metadata and account files unrecoverable.

The cache is process-local only, limited by entry count and approximately 16 MiB of serialized keys/values, with configurable TTL and periodic cleanup. It contains read results, not media bytes, and every request is authorized before a lookup; writes invalidate the account and in-flight reads cannot repopulate after invalidation. Only one gateway instance/worker is supported: in-memory caches and simulator state are not distributed. The application takes a cooperative PostgreSQL session advisory lock, or a POSIX sibling lock file for local SQLite, to detect a second cooperating instance. This is not distributed fencing and does not protect against noncooperating writers, file aliases, or effects already in flight; see `OPERATIONS.md`. Synthetic media age-based retention deletes bytes on access and at startup/approximately hourly; message metadata remains. `null` disables age-based expiry but is not a backup. Removal from the active state is not secure erasure from filesystem journals, snapshots or backups.

## Session-monitor metadata

The `session_monitor_v1` setting stores the most recently observed normalized state by enabled account UUID, and `session_transition` audit rows record state changes in the same database transaction. This contains no message content, credentials, phone numbers or upstream error strings. Unchanged states remain deduplicated across process restarts; audit retention and backups apply to transition rows. The additive `0004` migration indexes `(event, id)` for owner-filtered history. Monitor task progress, counters and timestamps are process-local and reset at restart; they are not a durable heartbeat. Multiple gateway workers remain unsupported.

## Event inbox and subscriptions (migration `0005`)

`events` is the durable normalized inbox: an auto-incrementing per-gateway `seq`, immutable UUID `id`, account UUID, namespaced `event_type`, `schema_version`, source, `occurred_at`/`observed_at`, aggregate identifiers, optional `chat_id`, correlation/causation IDs, a schema-validated JSON `payload` (≤ 8 KiB, allowlisted keys per type) and a unique `fingerprint` for deduplication. Raw TDLib objects are never stored. `event_subscriptions` holds the client/account filters, pause flag, `max_inflight` and the monotonic `cursor_seq`. `event_attempts` bounds per-subscription delivery attempts; `event_dead_letters` records poison events with a sanitized reason. Event payload retention is separate from audit and media retention and is pruned only by the owner endpoint.

## Jobs and execution history (migration `0006`)

`jobs` stores the registered `kind`, versioned strict `input` (≤ 4 KiB, no credentials or message bodies), creator `client_id`/`credential_id`, account, schedule (`once` with an aware `run_at`, or `daily_local` with IANA zone and local time), misfire policy/grace, state, attempts, deadline, lease owner/expiry, pause and cancel flags, last sanitized error, bounded result and unique idempotency key. `job_runs` keeps one bounded row per attempt (state, worker, timings, error code, unknown-outcome flag). Neither table stores message text from previous runs.

## Media staging

Staged media bytes are not stored in PostgreSQL. They live under a private staging root in per-account UUID directories with server-generated names, mode 0600, written to a `.part` file and atomically renamed after size/signature/SHA-256 verification. Metadata (filename, content type, size, checksum, account) exists only in process memory for the duration of a transfer and is discarded on release. Files expire after the staging TTL and are removed on failure, cancellation, release and startup/hourly cleanup. Backups do not capture staged bytes.

## Command audit

`command_save` audit events store the account, source chat and decision/error code. Message text, captions, filenames, media checksums and staging identifiers are never written to audit, logs or MCP responses.
