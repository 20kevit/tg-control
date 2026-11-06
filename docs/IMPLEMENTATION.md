# Independent implementation baseline

Base: f9b3fa6776a1951430b55603266d68d42e2b18b8. Session branch:
`arena/9088bfcd-tg-control`; fixed by the execution environment, deviating from
requested `arena-agent-2`. No other agent implementation was consulted.

## PRD and scope
The normative requirements are README.md and ARENA.md. Synthetic-only personal
MTProto gateway, multiple accounts and clients, independent Read/Write/Admin,
exclusions overriding grants, shared core for interfaces. No Bot API. No automatic
message interpretation. `/save` is explicit and reply-aware, requires source Read
and destination Write, and preserves content without interpreting it.
AI utility catalog remains owner-defined; only bounded task contracts are built.

## Architecture / access control
Interfaces -> Gateway -> policy -> account adapter. A batch is fully authorized
before any adapter side effect; outcomes are independent, not transactional.
Each grant keys client, account and permission; public chat types can be broad
grants, private chats require explicit IDs. Exclusions win. Account identity and
chat classification come from the adapter, not caller claims. Production metadata
must use PostgreSQL; memory stores are exclusively a synthetic test harness.
Credentials store salted one-way digests, independently revocable. Audit records
contain identifiers and outcomes, never message bodies or credential material.

## Data model / API contracts
Account(id, alias, state_path); Client(id); Credential(id, client, digest, revoked);
Policy(client, account, permission, chat_ids, public_types, exclusions);
Audit(client, account, operation, chat, outcome, timestamp).
Gateway execute accepts client identity established by an interface, explicit
account, operation and chat. Search only traverses authorized chats. `/save`
accepts command message ID and source chat; it resolves reply through the adapter.
Media pipeline uses in-memory bytes in fixtures, preserving captions/filenames;
production streaming, size limits, safe file naming and cleanup remain required.

## Security / threat model
Telegram content is opaque untrusted data. No dispatch from incoming text. No
provider network integration or real TDLib login exists. Fail closed on unknown
accounts, clients, operations and grants. Never accept caller-provided provenance
as sufficient to unlock live data. Local interfaces must default to loopback;
remote MCP requires per-request authentication and exact Origin validation.
No public server is started by this implementation.
Retention defaults: audit 90 days; message cache disabled; media ephemeral.
Production state directories must be owner-only, encrypted at rest via encrypted
volume; encryption keys stay outside backups and repository in a secret manager.

## Compliance assessment (2026-11-06)
Gate CLOSED. Personal use and explicit invocation are not legal exemptions.
Official references: https://core.telegram.org/api/terms,
https://telegram.org/tos/content-licensing?setln=en,
https://core.telegram.org/tdlib.
Outbound host restrictions prevent independently fetching these official pages in
this environment. Current terms research is NOT complete; no claim of current
legal review is made. Owner must record a dated decision grounded in qualified
legal advice or written Telegram clarification, identifying permitted data,
purposes, providers, retention and restrictions. Approval is not implemented as a
user-editable Boolean. No live path is enabled even after editing configuration.

## Feasibility / ADR 001
Python standard library synthetic adapter first; no binding selected without a
native-runtime proof. TDLib JSON interface is the proposed future native boundary.
One native client, database and files directory per account. Mock isolation is not
native runtime validation. Native build/runtime feasibility remains unverified.
No Redis: no measured need. PostgreSQL schema is a draft, not a deployed migration.

## Roadmap and measurable gates
0: baseline docs and contract. 1: verified official terms and native feasibility.
2: native JSON boundary isolation/error tests without login. 3: PostgreSQL-backed
metadata/credentials/audit and migrations integration tests. 4: read/search policy
tests. 5: write/save/batch retry tests. 6: authenticated CLI/MCP/Web Admin integration
and management. 7: dependency/security checks, Docker and recovery rehearsal.
Synthetic foundations do not constitute completion of these production gates.

## Development / testing / operations
Run `python -m unittest discover -s tests -v`. Never supply real session material.
Tests must cover permission independence, exclusions, private defaults, account
isolation, batch preflight, rate-limit timeout and save failure paths.
Before production: dependency audit, PostgreSQL integration, backup/restore with
isolated restore database and owner approval, TLS/Origin/auth review, native
binding validation. No VPS/DNS/firewall operations are authorized. Back up encrypted
metadata and per-account state together; validate recovery without live network.

## Interface and command catalog
`tg-control demo` runs synthetic read and reply-save through Gateway. `/save` is
never inferred from incoming text. MCP and Web Admin are not yet implemented;
their tool descriptions must specify explicit account, scoped operations and
opaque untrusted results. Generic AI task adapter accepts explicitly selected
message IDs only, Read-authorizes each, and rejects non-synthetic adapters.
Candidates for later owner confirmation: bounded translation or transcription;
no utility catalog or provider is currently enabled.

## Traceability
Core/policy: tests/test_core.py. Reply/save/media: same test suite. Credential
lifecycle: tests/test_security.py. PostgreSQL baseline: migrations/001_initial.sql.
Unimplemented acceptance requirements are recorded in DELIVERY.md, not waived.
