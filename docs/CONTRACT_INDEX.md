# Product and engineering contract index

Updated: 2026-11-07. This document is the navigation and precedence contract. It does not override the Telegram compliance gate or owner approval boundaries.

## Purpose

The repository must let Arena continue through all approved engineering work without routine owner questions. Arena inspects the code, chooses reversible implementation details, records consequential choices as ADRs, implements, tests, updates documentation, commits, and continues.

A document, interface stub, mock, or unit test alone does not complete a requirement. Use the status vocabulary below and the evidence in ACCEPTANCE.md.

## Authority and conflict resolution

Apply this order when documents conflict:

1. Latest explicit owner decision recorded in the project requirements/Notion project record.
2. Explicit product invariants and approval/compliance gates in ARENA.md, PRD.md, and COMPLIANCE.md.
3. Detailed domain contracts linked below.
4. Architecture, API, security, data-model, operations decisions, and accepted ADRs.
5. Roadmap, status reports, examples, and historical notes.

A dated historical status entry is evidence of what was true then, not a current requirement. Update stale statements when a later accepted decision supersedes them. Never infer owner approval from competitor research or implementation convenience.

## Requirement status vocabulary

- Confirmed: explicitly requested or approved by the owner.
- Required foundation: engineering capability needed to support confirmed behavior; Arena implements without asking.
- Candidate: optional feature found in research, not approved as user-facing behavior. Do not silently promote it to Confirmed.
- Blocked: cannot proceed past a legal, security, external-service, or owner-approval gate.
- Implemented: code exists.
- Verified: relevant tests/evidence were actually executed. Synthetic/mock evidence must be labelled as such.

Every feature matrix row identifies status and links to implementation and tests. One status must not imply another.

## Contract map

| Area | Authoritative document | Scope |
|---|---|---|
| Product behavior | PRD.md, ARENA.md | Accounts, clients, access, read/write/admin, commands, AI boundaries |
| Architecture | ARCHITECTURE.md, ADR.md | Core, adapter, storage, interfaces, process topology |
| External API | API.md, MCP.md, CLI.md, authenticated OpenAPI | Versioned operations, schemas, errors, clients |
| Authorization | SECURITY.md, POLICY.md | Principals, grants, scopes, exclusions, preauthorization |
| Data and retention | DATA_MODEL.md, OPERATIONS.md | Metadata, isolation, encryption, cache, audit, backup |
| Events/jobs/workflows | EVENTS_JOBS_WORKFLOWS.md | Event delivery, durable jobs, scheduling, workflow execution |
| Commands/media/AI/plugins | COMMANDS_MEDIA_AI_PLUGINS.md | Command routing, /save, transfer, AI task adapters, plugin lifecycle |
| Compliance | COMPLIANCE.md | Telegram terms, AI data-use restrictions, release gates |
| Delivery/evidence | ROADMAP.md, STATUS.md, ACCEPTANCE.md, TESTING.md | Stages, traceability, tests, blockers |

When a contract is implemented, update its source document and linked API/help/acceptance material in the same change.

## Confirmed product scope

- Personal Telegram accounts through MTProto/TDLib, not Bot API.
- Multiple isolated Telegram accounts from the initial architecture.
- Multiple separately authenticated AI clients, each with account-specific grants.
- MCP, CLI, HTTP API, and Web Admin over one Gateway Core; no interface bypasses Core or Policy Engine.
- Independent Read, Write, and Admin permissions; exclusions override grants; private chats require explicit scope.
- Read/search/history/media retrieval within authorized scopes; supported writes include send, edit, delete, forward, and media; Telegram Admin operations require Admin.
- Cross-account workflows are supported only where each target is independently authorized.
- Batch operations preauthorize every target before effects and return correlated per-item outcomes; they are not atomic.
- /save is an owner-invoked reply-aware command that saves the replied-to message content to Saved Messages of the same account through the shared command/core/policy/media/audit path.
- A small set of explicit, narrow AI utilities may be implemented using provider-independent task adapters. No broad background analysis or arbitrary message-triggered AI execution.
- User-configured automation is opt-in and cannot weaken permissions or trust boundaries.

## Required foundations Arena implements without asking

1. Stable, versioned domain and external API contracts; consistent pagination, error, timeout, cancellation, idempotency, and compatibility behavior.
2. Complete account-scoped adapter lifecycle, normalized updates/events, session-state handling, and truthful mutation outcomes.
3. Durable event delivery with deduplication, consumer checkpoints, replay/resume, retry, and poison-event isolation.
4. Durable jobs/scheduler with explicit state transitions, restart recovery, timezone handling, safe retries, cancellation, and execution history.
5. Workflow/command registration with explicit triggers, typed inputs/outputs, bounded execution, and authorization at execution time.
6. Media transfer lifecycle with streaming, validation, checksums, quotas, resumability, cleanup, retention, and storage adapters.
7. Provider-independent AI task contract with explicit invocation, strict input scope, output schemas, budgets, timeout/cancellation, and compliance enforcement.
8. First-party plugin contract with versioned manifest, capability grants, lifecycle, health, audit, and no policy bypass.
9. Security/operations foundations: secret lifecycle, key rotation/recovery, observability, alerting, migration/restore drills, graceful shutdown, dependency/supply-chain checks, and production runbooks.
10. Requirement-to-code-to-test traceability and end-to-end synthetic acceptance tests for every user-visible capability.

## Autonomy and stop conditions

Arena continues through Confirmed and Required foundation work: refactors, tests, documentation, migrations on disposable test databases, synthetic fixtures, and local reproducible builds.

Stop and report only for:
- Real Telegram login, authorization, synchronization, or real-data ingestion.
- Sending real Telegram content to an AI provider.
- Resolving/interpreting the legal or compliance gate.
- Real credentials, production secrets, owner key custody, or external account access.
- Production/VPS deployment, DNS, tunnel, firewall, ingress, external exposure, or infrastructure changes.
- Destructive/irreversible actions or migrations against non-disposable data.
- Changing product/security invariants, permission semantics, data-use policy, or approved feature scope.
- A genuinely unresolved owner-facing choice that changes user-visible behavior or legal/security posture.

For ordinary engineering choices, choose the safest conventional option, document consequential choices in an ADR, and continue. If user-facing semantics are not approved, implement reusable foundations and synthetic tests, mark the feature Candidate, and continue other work.

## Candidate feature backlog

Competitor research identified possible modules such as AFK/status replies, keyword/regex auto-replies, reminders, downloader queues, voice transcription, text-to-speech, OCR, translation, formatting, channel reactions, profile automation, chat export, and workflow templates. These are Candidates, not automatically approved behavior. Arena may prepare shared foundations and synthetic adapters, but must not activate or ship a user-facing candidate without owner confirmation. No candidate permits broad background AI analysis, arbitrary message-content instruction execution, or crossing the compliance gate.

## Completion standard

The confirmed product is complete only when:
- Each Confirmed and Required foundation item has traceable implementation and acceptance tests.
- All approved commands and AI utilities work end-to-end through shared Core/Policy/Media/Audit paths.
- Events/jobs/workflows/plugins in confirmed scope meet their domain contracts.
- All interfaces expose consistent schemas and outcomes.
- Negative authorization, isolation, failure, restart, retry, cancellation, and security tests pass.
- Packaging, backup/restore, monitoring, key management, and recovery are evidenced.
- Remaining Candidates are clearly listed, not falsely claimed complete.
- Real-data/release items remain blocked until compliance and owner gates are satisfied.

Current state and next executable stage are in STATUS.md and ROADMAP.md.
