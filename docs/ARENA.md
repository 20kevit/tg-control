# Arena Execution Contract

## Mission

Implement tg-control as a reliable personal Telegram AI Gateway. Arena is the autonomous engineering executor. The project owner and reviewing architect retain authority over product invariants, security boundaries, compliance decisions, and approval-gated operations.

Arena must inspect the repository and existing project context before making changes. Convert the requirements below into maintained documentation, a staged backlog, implementation, tests, and reviewable commits. Continue automatically through every stage that does not require owner approval. Do not stop after a plan, scaffold, or progress summary.

## Product baseline

- This is a gateway for personal Telegram user accounts using MTProto/TDLib, not Telegram Bot API.
- Multiple Telegram accounts are supported from the initial architecture. Each account has isolated TDLib state/database and an alias plus account identity.
- Multiple AI clients are supported. Each client has its own revocable credential and per-account authorization.
- Every request identifies its target account or authorized account set.
- MCP, CLI, and Web Admin are required interfaces. All must use the same Gateway Core/API; none may access TDLib directly.
- Gateway Core owns request validation, routing, normalized results, and the single execution path.
- A server-side Policy Engine enforces every operation.
- PostgreSQL is the baseline for gateway metadata, clients, credential metadata, policies, configuration, and audit. TDLib owns Telegram synchronization/state and per-account local data. Add Redis or other infrastructure only with evidence.
- Docker-based portability is required. Initial VPS deployment is a future gated operation, not part of unapproved work.

## Authorization invariants

1. Read, Write, and Admin are independent permissions.
2. Read never grants Write. Admin does not implicitly grant Read or Write.
3. Read and Write have independently configurable scopes and exclusions.
4. Explicit exclusions always override broad grants.
5. Default Read policy may include public channels and public groups. Private groups and private one-to-one chats require explicit permission.
6. New chats inherit only the configured policy for their chat type and scope.
7. Search is part of Read and is limited to readable chats; it is not a separate permission.
8. Write is one permission covering send, edit, delete, forward, and media operations.
9. Admin is one independent permission covering Telegram administrative operations.
10. Bulk operations across authorized chats/messages/members are supported. Pre-authorize all targets, then report per-item outcomes; do not imply cross-chat transactions.
11. If an operation is authorized, do not add redundant confirmation prompts by default. Enforce authorization in the gateway itself.
12. Respect Telegram/platform permissions and limits. Automatically wait/retry for rate-limit durations where safe and report final outcomes/timeouts.

## Security and data handling

- Treat Telegram message bodies, captions, filenames, attachments, and metadata as opaque untrusted data, never as instructions.
- No message-triggered actions, permission changes, or automatic semantic command execution. User-defined rules are a future opt-in feature and must be separately designed.
- Never commit credentials, session secrets, raw tokens, or live Telegram content. Do not expose secrets in logs or model context.
- Use unique credentials per AI client, with rotation and revocation.
- Maintain a detailed local audit trail of requests and outcomes. Default retention is 90 days and configurable. Client-visible outputs must be filtered and must not expose credentials. Avoid duplicating full message bodies into audit records without a documented necessity.
- Message cache is configurable and temporary by default. Media/file retention is configurable, including permanent retention only when explicitly configured.
- Isolate each account's TDLib database and files. Protect local state and backups; document encryption/key handling without committing keys.
- Handle session expiry/disconnect with bounded reconnect/retry. If re-authentication is required or recovery fails, report clearly and request owner action.
- Validate remote MCP authentication on every request and validate Origin. Local stdio/local listeners must not be exposed on public interfaces by default.

## Mandatory compliance gate

Telegram's API Terms and Content Licensing terms contain restrictions involving use of Telegram data in AI/ML systems. Whether the intended forwarding of message content to external AI clients is permitted in this exact personal-use scenario is unresolved. Do not infer permission from the personal nature of the project or from treating messages as untrusted data.

Until the owner records a resolved compliance decision based on qualified legal advice or written clarification/permission from Telegram:
- Use synthetic fixtures and test data only.
- Do not log in to a real Telegram account through this project.
- Do not ingest, cache, export, or forward live Telegram content to AI clients.
- Do not build or run a live-data demonstration.

Reference terms:
- https://core.telegram.org/api/terms
- https://telegram.org/tos/content-licensing?setln=en
- https://core.telegram.org/tdlib

Arena may research and document the issue, but must not make the legal decision on the owner's behalf.

## Architecture direction

Use a Telegram Adapter boundary around TDLib, with one isolated TDLib client/database per account. The adapter handles authentication state, updates, Telegram operations, retries, and rate limits.

Gateway Core is the only path from external interfaces to Telegram operations. The Policy Engine authorizes each request against client identity, account, chat scope, operation type, and exclusions before execution.

MCP, CLI, and Web Admin are separate client interfaces over the shared Gateway Core/API. Suggested MCP transports are local stdio and remote Streamable HTTP, subject to security review. Python is the preferred baseline for Gateway Core, policy engine, API, and CLI; native TDLib integration remains behind the adapter. Validate the selected binding and multi-account behavior with a synthetic-data proof of concept before committing to broad implementation.

## Staged delivery

### Stage 0 — Repository and requirements baseline
Create and maintain PRD, architecture, security, access-control, data model, API contracts, ADRs, compliance assessment, roadmap, development guide, test strategy, and this execution contract. Record assumptions and distinguish owner decisions from engineering choices.

### Stage 1 — Compliance and feasibility
Research current official Telegram terms and TDLib capabilities. Produce a concise compliance risk assessment and technical feasibility report. Do not proceed to live-data work. Define the precise owner approval required to pass the compliance gate.

### Stage 2 — TDLib proof of concept
Build a minimal adapter/feasibility prototype using synthetic fixtures or mocks only. Verify build/runtime integration, account isolation design, and error-state handling without authenticating a real account.

### Stage 3 — Gateway foundation
Implement configuration, domain models, account/client metadata, credential lifecycle abstractions, storage migrations, request validation, policy engine, audit foundation, and adapter interfaces.

### Stage 4 — Read-only vertical slice
Implement read/search flows against mocks or synthetic adapter fixtures. Demonstrate per-client/per-account authorization, scope enforcement, exclusions, audit events, normalized errors, and tests.

### Stage 5 — Write capability
Implement write operations behind policy enforcement and synthetic/mock adapter tests. Include batch pre-authorization, per-item outcomes, retry/rate-limit behavior, and failure tests. Do not enable live Telegram operations without approval.

### Stage 6 — Client interfaces
Implement MCP, CLI, and Web Admin against the shared Gateway Core/API. Include auth boundaries, policy management, account/client management, audit views, and cache/retention settings. Do not expose live credentials or session material to clients.

### Stage 7 — Hardening and operations
Complete security review, dependency checks, integration tests, backup/recovery documentation, observability, packaging, and deployment runbooks. Do not deploy to the owner's VPS or change DNS, Cloudflare, firewall, or production configuration without explicit approval.

## Approval gates — stop and ask the owner

Stop before:
- Any real Telegram account login, live Telegram content ingestion, or forwarding live content to AI.
- Any production deployment, VPS modification, DNS/Cloudflare/firewall change, or external exposure.
- Any destructive operation or irreversible migration.
- Creating, transferring, or using real credentials/secrets outside approved local test fixtures.
- Changing product invariants, permission semantics, trust boundaries, or security model.
- Making the repository public or changing repository sharing/access.
- Treating the compliance gate as resolved.

Routine, reversible engineering decisions are delegated to Arena. Choose sensible defaults, document them in ADRs, and continue. Do not interrupt the owner for ordinary library choices, module organization, test implementation details, or refactors that preserve invariants.

## Blocker protocol

When blocked, provide:
1. Exact blocker and evidence (command/output, failing test, source, or missing approval).
2. Impact on the current stage and downstream work.
3. Safe options and trade-offs.
4. Recommended path.
5. Work that can continue in parallel without crossing a gate.

Do not silently weaken security, scope, or acceptance criteria to get a green build.

## Definition of done

A stage is complete only when:
- Its acceptance criteria are measurable and satisfied.
- Relevant tests and quality checks have actually been executed; commands and results are recorded.
- Error, timeout, retry, audit, and authorization behavior are covered where applicable.
- Documentation matches the implementation.
- No credentials, secrets, or live Telegram data are present in the repository or test artifacts.
- No known critical/high severity security issue remains unaddressed.
- Changes are committed in small, reviewable commits.

At each stage boundary, report delivered files/features, exact tests and results, security review findings, known limitations/blockers, and the next stage. Then continue automatically if no approval gate is reached.

## Reviewer responsibilities

The owner/reviewer evaluates product correctness, architecture, security invariants, compliance, stage evidence, and approval-gated actions. The reviewer should challenge unsupported claims and request concrete evidence. Arena owns routine implementation, debugging, tests, and documentation updates.


## Product clarification addendum — reply-to-save and bounded AI utilities (2026-11-06)

This addendum refines the product baseline and is authoritative where it is more specific. Preserve all existing authorization and compliance invariants.

### Reply-aware `/save` command

Implement an owner-invoked `/save` command that acts on the message to which the command message replies:

- Resolve the replied-to message in the explicitly selected Telegram account.
- Download the content of the replied-to message and send it to that same account's Saved Messages (chat with self), preserving captions and filenames where supported.
- Support ordinary Telegram media/content types exposed by the adapter. Return a clear result for missing reply, unsupported content, unavailable media, download/upload failure, permission denial, and rate limiting.
- Route the command through the shared reply-aware Command Router, Gateway Core, Policy Engine, Media Pipeline, and audit path. No direct-TDLib shortcut or permission bypass.
- Enforce the account/client authorization and Write permission for the destination operation.

Use the standard Telegram account and media operation flow. Handle unavailable content and API errors clearly.

Acceptance tests must cover reply resolution, text and media content, caption/filename preservation, missing reply, unsupported content, unavailable media, failed download/upload, denied Write permission, rate limits, and strict per-account isolation.

### Narrow, explicit AI utilities

The product is not intended to perform broad autonomous analysis of all Telegram conversations by default.

- Implement a small set of purpose-built AI tools only. Each tool must have an explicit user/client invocation, a narrow purpose, defined input/output schema, bounded scope, and a documented permission check.
- Do not add automatic semantic analysis of every incoming message, background conversation monitoring for AI, autonomous interpretation of message contents, or message-triggered AI actions.
- Keep provider integrations behind provider-independent task adapters. The adapter layer may be implemented and tested with synthetic fixtures while the compliance gate remains closed.
- Do not assume that a narrow utility or user initiation makes Telegram-derived data use compliant. Until the existing compliance gate is formally resolved, do not send real Telegram text, voice, images, or other media to any AI provider. Keep live-data AI execution disabled by default.
- Do not invent an approved AI utility catalog. Record candidate tools and ask for owner confirmation only where the exact user-facing behavior is genuinely unspecified; continue building generic adapter contracts and synthetic tests meanwhile.

Acceptance tests must prove that AI tools run only on explicit invocation, receive only the authorized/specified input, cannot access unrelated chats or accounts, do not trigger from arbitrary message contents, and remain blocked from real Telegram-derived data until compliance approval is recorded.

### Documentation and delivery updates

Update the PRD/feature matrix, command catalog, architecture, media pipeline contract, threat model, compliance assessment, API/MCP tool descriptions, CLI help, Web Admin documentation, and test plan to reflect this addendum. Add traceability from each requirement to implementation and tests. Keep the implementation incremental; establish reusable interfaces now without expanding the initial feature scope unnecessarily.


## Competitive gap review addendum — execution guidance (2026-11-06)

This addendum translates the competitor review into engineering direction. It is subordinate to the product baseline, authorization invariants, approval gates, and mandatory compliance gate above. Competitor feature lists are research inputs, not approved product requirements.

### Current repository reality

The reviewed repository snapshot contains documentation only (README.md and docs/ARENA.md). Do not describe planned architecture as implemented. First establish a runnable, tested foundation and keep a traceable distinction between planned, scaffolded, implemented, and verified capabilities.

### Engineering priorities

#### Priority 0 — executable foundation

- Establish a maintainable application layout, dependency management, configuration validation, database migrations, structured logging, and reproducible local Docker development.
- Define versioned domain/API contracts before implementing multiple client interfaces. Specify request/response envelopes, normalized error codes, pagination/cursors, timeout/cancellation behavior, and compatibility policy.
- Define client authentication and credential lifecycle: issuance, secure storage, rotation, revocation, and per-request identity resolution. Never return raw secrets after issuance.
- Implement the account registry and isolated Telegram Adapter interface; prove TDLib binding/build/runtime feasibility with mocks or synthetic fixtures only.
- Implement the Policy Engine as a deny-by-default server-side boundary. Add a permission/scope matrix and negative tests for Read, Write, Admin, exclusions, account isolation, and chat types.
- Add automated unit/integration tests, lint/type checks, CI, and a synthetic-data smoke test. Document exact commands and results.

#### Priority 1 — reliable read-only slice and event contracts

- Deliver one end-to-end read-only path through the shared Gateway Core, initially backed by deterministic fixtures/mocks.
- Define normalized event envelopes with stable IDs, account identity, timestamps, schema version, deduplication behavior, and explicit ordering guarantees (or explicitly state when ordering is not guaranteed).
- Specify subscription filters, consumer acknowledgements, retry/backoff, replay/resume cursors, poison-event handling, and dead-letter behavior before exposing event delivery.
- Specify stable MCP tool schemas and CLI commands over the same Core contracts. Add HTTP/OpenAPI only as part of the agreed API surface; do not create a second business-logic path.
- Test authorization before data retrieval, not only before formatting a response.

#### Priority 2 — durable jobs and media lifecycle

- Define durable job states and transitions, persistence, timezone-aware schedules, misfire behavior, pause/resume/cancel, concurrency limits, retry/backoff, execution history, and restart recovery.
- Re-check client/account/chat authorization when a delayed job executes. Define idempotency and duplicate-send handling for operations whose outcome is uncertain.
- Define media transfer contracts: streaming/chunking, resumability, size and quota limits, MIME/content validation, checksums, duplicate handling, temporary-file cleanup, retention, and storage adapters.
- Keep optional media processing (such as transcription or OCR) separate from basic transfer and behind explicit task invocation and the existing compliance gate.

#### Priority 3 — controlled writes and operator experience

- Implement Write operations only through Gateway Core and Policy Engine, with batch pre-authorization, per-item results, rate-limit handling, and failure/retry tests.
- Add operational health/readiness checks, correlation IDs, metrics, tracing where justified, queue and job visibility, graceful shutdown, and actionable diagnostics.
- Document backup/restore and migration/rollback procedures; exercise recovery in a disposable test environment.
- Build Web Admin against the same Core/API. Protect browser sessions and state-changing requests; validate Origin/CSRF protections and never expose TDLib session material.

#### Priority 4 — extensibility after stable core

- Specify a versioned first-party plugin contract: manifest, compatibility, registration points, permissions/capabilities, lifecycle, health, configuration, and audit.
- Plugins must use approved Core interfaces and must never bypass Policy Engine, account isolation, audit, or compliance gates.
- Start with trusted first-party modules. Do not load arbitrary third-party code in-process until a separate isolation and threat-model review is complete.
- Add only owner-selected, narrow automation modules after the core is reliable. Do not expand scope merely to match competitor feature counts.

### Required engineering artifacts

Maintain these as concise, version-controlled documents and keep them aligned with implementation:

- docs/PRD.md and a feature/status matrix (planned / scaffolded / implemented / verified).
- docs/ARCHITECTURE.md and ADRs for consequential technical choices.
- docs/API.md or an OpenAPI specification plus MCP tool schemas.
- docs/SECURITY.md with threat model, trust boundaries, and security controls.
- docs/POLICY.md with permission/scope truth table and default-deny semantics.
- docs/EVENTS.md, docs/JOBS.md, and docs/MEDIA.md for their durable contracts.
- docs/OPERATIONS.md for configuration, health, backup/restore, migrations, and recovery.
- A test strategy and requirement-to-test traceability matrix.

Do not create empty documents just to satisfy this list. Create each artifact when its content is sufficiently defined and link it from the project documentation.

### Stage exit evidence

At every stage boundary, provide a compact evidence report containing:

1. Requirements delivered and their status (planned/scaffolded/implemented/verified).
2. Changed files and commit references.
3. Exact test, lint, type-check, build, and smoke-test commands with pass/fail results.
4. Authorization/security negative-test coverage and unresolved findings.
5. Operational limitations and known failure modes.
6. Any approval gate reached and the exact decision required from the owner.

A plan, interface stub, mock-only demonstration, or passing unit test must not be presented as a complete end-to-end integration. Label mock/synthetic coverage explicitly. Continue autonomously through reversible engineering work; stop only at the existing owner-approval gates.

### Explicit non-goals for this review

- Do not add broad background AI analysis, automatic interpretation of incoming messages, or autonomous message-triggered actions.
- Do not infer owner approval for new user-facing features from competitor examples.
- Do not weaken or bypass the Telegram AI-data compliance gate.
- Do not perform real Telegram login, live-data ingestion, live AI processing, production deployment, or external infrastructure changes without the approvals already required above.
