# ADR 002: PostgreSQL repository and commit-before-effect audit

Status: accepted engineering choice, 2026-11-06. Compliance gate unchanged.

## Decision
Use psycopg 3 with parameter binding, transactions and explicit connection lifetime.
One store serializes its connection with an RLock; independent workers use separate
connections. Statement/lock/connect/idle-transaction timeouts are bounded. No Redis
or SQLite fallback. In-memory classes remain synthetic unit-test facilities.

Migration 001 is packaged with Python resources and checksum tracked. An advisory
transaction lock serializes migration runners; DDL and version insertion commit
atomically. A mismatched checksum fails closed. Automatic destructive migrations
or downgrade operations do not exist. Migration is explicit, never a side effect
of constructing a repository. Only apply to a new approved database/schema.

Gateway policy is loaded from PostgreSQL on authorization, including disabled
client state. Revocation is effective at the next check; already executing
operations cannot be recalled. External interfaces must still authenticate every
request and may not accept client strings as identity. Telegram Admin is NOT owner
metadata-management authority. Repository management methods are trusted local
owner primitives until a separately authenticated management boundary exists.

Credentials use random per-token salts and scrypt digests. Raw tokens are returned
only from issuance/rotation; list operations return non-secret IDs and revoked
status. Rotation is one database transaction with client-then-credential lock order.
Concurrent rotation of the same token has exactly one winner. Revocation takes a
non-secret credential ID. Disabling a client revokes its existing credentials.
No real credentials are generated during tests; only disposable synthetic tokens.

Audit stores IDs, operation, outcome and server timestamp. Commit `accepted` before
adapter execution, then commit final outcome. Audit unavailability before execution
blocks the operation. If final success cannot be persisted, report
`audit_outcome_unknown`; never automatically repeat the write. An accepted record
without a terminal outcome is explicitly ambiguous, NOT proof of success/failure.
No cross-system transaction or exactly-once guarantee is claimed.

Settings are allowlisted integers with bounds. Retention pruning is an explicit
maintenance method, not a background action or migration. Message cache remains
disabled in this build; retention settings are metadata for future pipelines.

## Evidence and trade-offs
Tests use pgserver's bundled PostgreSQL in a new temporary directory, Unix sockets
only (verified `SHOW listen_addresses` is empty), and cleanup that fixture cluster.
They never read an external DSN. This tests real SQL, rollback, persistence across
connections, policy updates, rotation races, retention and outage behavior without
owner infrastructure or live Telegram. pgserver is test-only; production uses a
separately provisioned PostgreSQL instance. Native TDLib integration is unaffected
and still unverified. TLS/DB role provisioning and backup restore still require
operations work. No public listener or deployment is started.
