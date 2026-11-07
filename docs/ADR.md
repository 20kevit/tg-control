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
