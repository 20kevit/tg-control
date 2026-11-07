# Roadmap and acceptance checklist

Status is evidence-based, not a promise of completion. Stages may overlap only where safe. See `STATUS.md` for executed commands and `ACCEPTANCE.md` for the implemented-vs-outstanding matrix.

- [x] **0 Baseline**: read all existing files/history; maintain PRD, architecture, security, data model, API, ADRs, development/testing guides, roadmap and status; establish a reproducible synthetic-only test command.
- [x] **1 Compliance research & feasibility**: reviewed official terms and TDLib excerpts, documented risks, owner decision and method limitations; synthetic isolation tests and an offline native ABI probe passed. This completes the research stage, **not** the unresolved legal/owner gate.
- [x] **2 TDLib proof of concept (offline only)**: defined the replaceable interface and tested synthetic isolation, lifecycle/errors and correlation; built and probed two native client IDs without parameters or networking; added unwired, fail-closed schema models for visibility, text flags and send outcomes.
- [ ] **Native adapter / live behavior**: a real TDLib adapter, authorization, sync, protected-content handling and live end-to-end tests are not implemented. No real account/data until the separate compliance and owner gates are resolved.
- [x] **3 Foundation (synthetic)**: PostgreSQL models and migrations, authenticated clients, rotation/revocation, account-scoped independent permissions, exclusions, fail-closed audit and input validation, covered by negative tests.
- [x] **4 Read (synthetic)**: permitted discovery, history, cross-account search/message/media reads; private default deny, result filtering, exclusion and isolation tests.
- [x] **5 Write (synthetic)**: authorized synthetic send/edit/delete/forward/media and admin, pre-authorize all batch targets, per-item results, retry/deadline and failure tests.
- [ ] **6 Interfaces**: API, CLI, MCP local and remote, and owner panel share the core/API; authentication, Origin, input validation and end-to-end tests plus usage docs. Synthetic paths implemented and exercised; remote OAuth/ChatGPT interoperability remains unverified.
- [ ] **7 Hardening/operations**: security review, dependency and regression tests, PostgreSQL integration, reproducible containers, backup/restore/migration guidance, health/monitoring/troubleshooting, retention and failure recovery verified where safe. Synthetic cache/media retention, encrypted persistence, backup/restore, cooperative single-instance protection and background metadata-only session-transition monitoring are implemented and tested against disposable PostgreSQL; external alert delivery remains absent. Compose configuration has static checks only; container/image and production deployment verification remain.
- [ ] **Release / real data**: all applicable requirements mapped to verified tests, no unaddressed high/critical issues, qualified compliance decision and explicit owner approval. No real login/deployment in this engagement without those gates.

Open acceptance items are not implicitly completed by scaffolding or passing only SQLite tests. Additional mandatory capabilities and any technical limitations must be carried forward explicitly.


## Next executable engineering wave — synthetic-only contract completion

Arena should continue without routine owner questions through the following work. This wave does not authorize real Telegram login/data, live AI processing, production deployment, or external exposure.

- [ ] Reconcile STATUS.md, PRD.md, API.md, architecture, test docs, and all historical claims with the current implementation; keep one current feature/evidence matrix.
- [ ] Implement and test the offline TDLib adapter boundary and update/authorization lifecycle harness, without credentials, TDLib parameters, network access, or live account authentication. Keep any future live adapter disabled and inaccessible until compliance/owner gates pass.
- [ ] Implement normalized event persistence, deduplication, subscriptions, cursors, replay/resume, acknowledgements, backpressure, retries, and dead-letter behavior using synthetic fixtures.
- [ ] Implement durable job state machine, scheduler, timezone/DST and misfire behavior, pause/resume/cancel, restart recovery, idempotency, and unknown-outcome safety.
- [ ] Implement bounded workflow execution with explicit triggers, typed conditions/actions, per-step policy rechecks, loop prevention, dry-run, and execution history.
- [ ] Implement shared Command Router and synthetic /save end-to-end through Core, Policy, Media, and Audit. No direct adapter shortcut.
- [ ] Implement streaming Media Pipeline with quotas, MIME/signature validation, checksums, resumability where safe, retention, cleanup, cancellation, and revocation checks.
- [ ] Implement provider-independent AI task interfaces and fake/local provider conformance tests; prove exact-input-only, explicit invocation, budgets, cancellation, and hard failure for real Telegram-derived input while compliance is blocked.
- [ ] Implement first-party plugin manifest/registry/lifecycle/capability grants/health/audit and failure isolation. Do not load arbitrary third-party code.
- [ ] Complete versioned API/MCP schemas, pagination, normalized errors, timeout/cancellation, compatibility, and transport-specific auth contracts. Keep remote OAuth/ChatGPT interop as a separately evidenced open interface gate.
- [ ] Add requirement-to-code-to-test traceability and end-to-end synthetic acceptance for every Confirmed/Required item.

Stage exit requires exact commands/results, negative authorization tests, failure/restart/retry/cancellation coverage, docs synchronized with code, and a reviewable commit. Continue to the next safe item automatically. See CONTRACT_INDEX.md and the domain contracts.
