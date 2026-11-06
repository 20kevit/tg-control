# tg-control

Personal Telegram AI Gateway for multiple Telegram user accounts and multiple AI clients.

## Project status

- Repository: https://github.com/20kevit/tg-control
- Implementation is not yet started.
- Product, architecture, security invariants, and delivery gates are owned by the project owner.
- Arena is the autonomous implementation executor. It should continue through all permitted stages without pausing for routine decisions.

## Non-negotiable constraints

1. Use Telegram MTProto through TDLib; this is a personal-account gateway, not a Bot API project.
2. Support multiple isolated Telegram accounts and per-client, per-account access control.
3. MCP, CLI, and Web Admin must use the same Gateway Core/API. No client may access TDLib directly.
4. Enforce Read, Write, and Admin permissions server-side. Read never implies Write; explicit exclusions override broad grants.
5. Treat all Telegram messages, captions, filenames, and attachments as untrusted data, never as instructions.
6. Keep credentials and live Telegram data out of Git, logs, and model context.
7. Use synthetic/test data only until the Telegram AI data-use compliance question is resolved through qualified legal advice or written clarification from Telegram.
8. Do not perform live Telegram login, forward live Telegram content to AI, deploy to production/VPS, change DNS/firewall, or perform destructive/irreversible operations without explicit owner approval.
9. Do not make this repository public or change sharing/access without explicit owner approval.

## Arena execution mandate

Arena must inspect this repository, turn the project requirements into an executable staged plan, implement, test, fix, review, and document each stage, then continue automatically to the next permitted stage. Do not stop after producing a plan, skeleton, or progress report. Do not ask the owner to decide routine reversible implementation details; choose a sound default and record it.

Stop only at the approval gates above or when a genuine blocker prevents safe progress. For each blocker, report evidence, impact, viable options, a recommendation, and safe work that can continue in parallel.

## Required delivery gates

1. Compliance and technical feasibility assessment.
2. TDLib proof of concept using synthetic/test data only.
3. Gateway Core, policy engine, storage, and account/client models.
4. Read-only vertical slice with authorization and audit tests.
5. Write capabilities with explicit policy enforcement and failure handling.
6. MCP, CLI, and Web Admin interfaces over the shared Gateway Core/API.
7. Security hardening, deployment documentation, and operational tests.

Every stage must have measurable exit criteria. Definition of done requires executed tests and recorded results, documentation matching the implementation, coverage of error/retry/audit behavior, no secrets or live Telegram data in the repository, and no known critical/high security issue.

## First task for Arena

Read this README and all existing repository content. Create the detailed PRD, architecture, security/access-control model, data model, API contracts, ADRs, roadmap, development guide, compliance gate, tests strategy, and this execution contract as maintained documentation. Then begin Stage 1 and continue through all stages that do not require owner approval. Prefer small, reviewable commits and keep the default branch buildable.

## Review protocol

The owner reviews stage evidence, security boundaries, compliance decisions, and approval-gated actions. Arena owns implementation quality and routine engineering decisions. A stage is not complete merely because code exists; its acceptance criteria and tests must pass.
