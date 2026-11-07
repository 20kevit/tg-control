# Architecture decision record

## ADR-001: Synthetic-first with an enforced live-data gate

Accepted. Telegram's AI-use restrictions are unresolved for this product. Implement and test with deterministic synthetic data; do not provide a configuration switch that silently enables real login. A future live adapter requires separately reviewed code, a recorded compliance decision and explicit owner approval.

## ADR-002: Python, PostgreSQL and a replaceable adapter

Accepted. Python provides mature API, security, migration and testing tooling. PostgreSQL is the authoritative metadata/audit store. SQLite may be used for isolated synthetic developer tests only; it is not production evidence. TDLib belongs behind an adapter; no AI-facing interface may call it directly. Redis is not justified at this stage.

## ADR-003: Separate owner authority from Telegram permissions

Accepted. A separately provisioned owner-management credential controls principals and policy. Telegram Read, Write, and Admin are three independent account/chat grants; Admin cannot grant gateway management or content access. Forwarding additionally requires source Read to prevent data exfiltration across scopes.

## ADR-004: Pre-authorization and retry safety

Accepted. Authorize every batch target before executing any item; fail the batch closed if a target is denied. Individual execution failures are reported without claiming atomicity. Retry only an explicit, definitely-not-applied rate-limit result, waiting the indicated duration within a finite deadline. Never blindly retry an outcome-unknown write.

## ADR-005: Cooperative single-instance guard, not distributed fencing

Accepted for synthetic development. Preserve the existing one-worker constraint with a dedicated PostgreSQL session advisory lock, or a private POSIX lock file for local SQLite. Refuse a second cooperating application and latch detected lock loss rather than silently reacquiring. This is a defense-in-depth check, not a multi-instance design: noncooperating writers, PostgreSQL transaction pooling/failover, SQLite path aliases and in-flight operations require separate controls. Backups still require all writers stopped.

## ADR-006: Pull-based durable event delivery with consumer cursors

Accepted. Persist every normalized adapter update in a gateway event inbox before acknowledging durable receipt, deduplicate by stable upstream identity (or a documented deterministic envelope fingerprint), and expose delivery as an explicit per-client subscription with a monotonic cursor. Delivery is at-least-once; consumers ack a sequence after durable acceptance, nack with a bounded attempt counter, and poison events move to a per-subscription dead letter while the cursor advances. Cursors that fall behind retained history return a typed `cursor_expired` error with the earliest resumable point instead of silently skipping. A consumer whose unacked backlog exceeds its configured in-flight limit receives `backpressure`; other consumers are unaffected. Operational (non-adapter) events are owner-only. Message content is delivered only after a current Read recheck for that chat. Rejected alternative: push/webhook/SSE transports. They require separate auth/signature/retry review and are not implemented.

## ADR-007: Durable jobs with leases, timezone-aware schedules and conservative recovery

Accepted. Jobs persist an explicit state machine (`queued`/`scheduled`/`running`/`retry_wait` and terminal states), attempts, bounded execution history, deadlines and a lease. Claims are conditional updates, so a second claimant cannot take the same job; the design still assumes one gateway instance and is not distributed scheduling. Recurring local schedules use IANA zones: nonexistent spring-forward times are skipped, and repeated fall-back times run once (fold 0). Misfires are resolved by a per-job `skip` or bounded `run_once` policy; no unbounded catch-up. Only RateLimited results proven not to have been applied are retried, within the deadline and after a permission recheck. Timeouts, lost leases and ambiguous adapter failures become `outcome_unknown`, which is terminal for automatic execution; an expired lease on a mutating kind is never retried. Handler kinds are registered explicitly; each input is a strict schema, and delayed Telegram writes execute through Gateway Core with the job's credential, which rechecks revocation, scope and exclusions immediately before the effect.

## ADR-008: Private, checksum-verified media staging without caller paths

Accepted. The Media Pipeline never accepts a filesystem path, URL or caller-chosen filename. Callers pass bytes already retrieved through Gateway Core plus untrusted metadata claims. Filenames and MIME types are validated (path separators, control characters, leading dot, trailing dot/space and length are rejected; only a small MIME allowlist is accepted) and file signatures are checked where a magic prefix exists. Bytes are written in bounded chunks to a server-generated name inside a private per-account directory using `O_EXCL|O_NOFOLLOW`, mode 0600, fsync'ed, re-read to verify the SHA-256 digest, and only then atomically renamed. Per-file, per-account, total-storage and concurrency limits apply; staged files expire after a TTL and are removed on failure, cancellation, release or startup/housekeeping cleanup. A staged reference is never a capability: the destination write is reauthorized by Gateway Core immediately before the send. The synthetic adapter supplies one in-memory chunk, so true network streaming is not exercised; the interface is chunk-based so a native adapter can stream later.

## ADR-009: Reply-aware commands routed through Core with explicit principals

Accepted. Commands are registered in one router with a stable name, version, explicit invocation mode, reply requirement and required permissions. Nothing triggers a command from incoming message content. `/save` resolves the replied-to message inside the explicitly selected account through the normal authorized Core read path, stages media through the shared pipeline, and sends the text/caption and media to the server-resolved Saved Messages chat of the same account through the same Core write path, using the caller's idempotency key. Retrieval requires Read for the source chat and the send requires Write for the destination chat; denials, unsupported content, missing replies, expired media, rate limits and unknown outcomes map to distinct normalized codes. The owner may invoke the command for an explicitly named client, which applies that client's policy rather than bypassing the Policy Engine. Audit records hold identifiers and outcomes only — never message bodies, captions, filenames or checksums.
