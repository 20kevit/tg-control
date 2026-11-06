# Architecture

Target flow: client (HTTP, CLI, MCP) → authenticated gateway API → request validation → account and per-operation policy → audit → normalized adapter interface → isolated account-specific Telegram client. The admin UI is an owner-authenticated API client, not an adapter client. Owner management authentication is separate from Telegram Admin permission; neither grants the other.

The current intended development adapter is synthetic only. Account IDs are gateway-generated UUIDs; a future TDLib integration must maintain one client, encrypted database, and files directory per account. The core never uses caller-supplied chat classifications: it resolves chat metadata in the selected account and checks policy before returning or mutating data. Account-wide discovery and search must filter every result. Forwarding requires Read on the source and Write on the destination, so a blind forward cannot extract unreadable content.

PostgreSQL stores metadata, credential digests, policy, settings, and audit. The simulator optionally persists encrypted, isolated synthetic source state; a bounded process-local read cache has configurable TTL and does not cache media bytes. Only one instance/worker is supported. A future TDLib adapter must be responsible for its own synchronization and state; gateway caching must never imply complete history. A bounded, explicit retry deadline applies only to failures known not to have been applied. An uncertain mutation must not be retried automatically. Batch authorization precedes all effects; successful earlier items are not rolled back if a later item fails.

The synthetic-only session monitor runs as a background task through Gateway Core and calls only adapter status, never requests or interprets messages or reconnects. The simulator's encrypted account document may nevertheless be loaded in full, including synthetic messages, to obtain its state. It atomically stores normalized state snapshots and metadata-only transition audit events. The owner panel fetches current status and an owner-authenticated, event-filtered history; the owner-only progress endpoint also reports in-process task liveness and pass timing; neither is an external notification system. Checks cannot run while the service is stopped, and cooperative timeouts cannot protect against an adapter that blocks the event loop or ignores cancellation.

## Trust boundaries

1. HTTP/MCP inputs and Origin/Host are untrusted; bearer identity comes only from the server credential store.
2. Telegram content, names and attachments are data, not instructions. No automatic execution, URL fetch or command evaluation occurs.
3. Owner management access is a distinct credential and cannot be acquired by granting Telegram Admin.
4. PostgreSQL and account files require protected persistent volumes, encrypted storage/backups, restricted access and independently managed keys. TLS termination belongs at an explicitly approved, correctly configured private edge.
5. Real Telegram data is a hard external gate, not an environment toggle. See `COMPLIANCE.md`.

See `ADR.md` for decisions, `DATA_MODEL.md` for entities and `SECURITY.md` for threat analysis. Implementation status and outstanding gaps are tracked in `STATUS.md` and `ROADMAP.md`.
