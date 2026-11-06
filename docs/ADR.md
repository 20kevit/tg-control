# Architecture decision record

## ADR-001: Synthetic-first with an enforced live-data gate

Accepted. Telegram's AI-use restrictions are unresolved for this product. Implement and test with deterministic synthetic data; do not provide a configuration switch that silently enables real login. A future live adapter requires separately reviewed code, a recorded compliance decision and explicit owner approval.

## ADR-002: Python, PostgreSQL and a replaceable adapter

Accepted. Python provides mature API, security, migration and testing tooling. PostgreSQL is the authoritative metadata/audit store. SQLite may be used for isolated synthetic developer tests only; it is not production evidence. TDLib belongs behind an adapter; no AI-facing interface may call it directly. Redis is not justified at this stage.

## ADR-003: Separate owner authority from Telegram permissions

Accepted. A separately provisioned owner-management credential controls principals and policy. Telegram Read, Write, and Admin are three independent account/chat grants; Admin cannot grant gateway management or content access. Forwarding additionally requires source Read to prevent data exfiltration across scopes.

## ADR-004: Pre-authorization and retry safety

Accepted. Authorize every batch target before executing any item; fail the batch closed if a target is denied. Individual execution failures are reported without claiming atomicity. Retry only an explicit, definitely-not-applied rate-limit result, waiting the indicated duration within a finite deadline. Never blindly retry an outcome-unknown write.
