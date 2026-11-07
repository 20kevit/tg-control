# Commands, media, AI tasks, and plugins contract

Status: command/media foundations are required. AI task infrastructure is required, but live Telegram-derived AI execution is compliance-blocked. Plugin foundations are required before third-party modules. Optional user-facing modules remain Candidates unless listed as Confirmed in PRD.md.

## Implementation status (2026-11-07)

| Contract area | State | Evidence |
|---|---|---|
| 1.1 Command model, registry, explicit invocation | Implemented for the single approved command | `src/tg_control/commands.py` (`COMMANDS`, `CommandRouter.catalog`); `tests/test_commands.py::test_command_catalog_is_explicit`, `test_unknown_command_is_rejected` |
| 1.2 Reply context, same-account resolution, typed errors | Implemented | `CommandRouter._read_message`; `test_save_resolves_reply_in_selected_account_only`, `test_save_missing_reply_and_invalid_input` |
| 1.3 `/save` end-to-end | Implemented (synthetic) | `CommandRouter._save`; `tests/test_commands.py`, `tests/test_commands_api.py`; every listed error class has a test except a dedicated authenticated timeout (covered at Core level) |
| 2.1–2.2 Media transfer contract | Partially implemented | `src/tg_control/media.py`; streaming/chunking, validation, checksums, quotas, staging, cancellation, TTL cleanup, reauthorization guard. Not implemented: resumability, client-facing byte streaming, checksum-based deduplication |
| 2.3 Media processing (transcription/OCR/etc.) | Not implemented (Candidate) | requires explicit task invocation and remains compliance-blocked |
| 3.1–3.2 AI task adapters | Implemented for the contract (synthetic) | `src/tg_control/ai.py`: strict schemas, capability-declared providers, explicit selection, budgets, timeout/cancellation, retry rules, output validation, sanitized audit; `tests/test_ai.py`, `tests/test_ai_api.py` |
| 3.3 AI utility catalog | Intentionally empty (Candidate) | application registers no task; `GET /v1/ai/tasks` returns `[]`; `test_catalog_has_no_approved_utility_by_default` |
| 4.1–4.3 Plugin manifest, lifecycle, capability grants | Implemented (first-party) | `src/tg_control/plugins.py`: strict manifest, install-without-execution, explicit grants, module binding, delegated-client execution, capability checks, rate/timeout containment, quarantine; `tests/test_plugins.py`, `tests/test_plugins_api.py` |
| 5 Candidate modules | Not approved | no user-facing candidate is activated; the AI catalog is empty and no plugin is installed |

Limits: no incoming-message trigger exists by design; the media pipeline has no resumable transfer or HTTP streaming download; `/save` supports only the synthetic adapter's already-materialized media; AI providers are synthetic only and no utility is approved; plugins are first-party in-process modules with no sandbox or remote distribution.

## 1. Reply-aware command router

### 1.1 Command model

Every command is registered with:
- Stable command name and aliases.
- Versioned input and output/result schemas.
- Explicit invocation mode: owner command, client tool, or approved workflow trigger.
- Required principal type and account/chat scope.
- Required Read/Write/Admin permission(s).
- Whether reply context is mandatory.
- Timeout, cancellation, rate-limit, and idempotency behavior.
- Help text, localization keys, and audit event types.

Commands dispatch through a shared Command Router into Gateway Core. CLI, MCP, HTTP, and Web Admin cannot implement separate command business logic. A command never executes merely because arbitrary incoming message text resembles a command. Message-triggered command execution stays disabled unless a separately approved, explicitly configured rule enables it.

### 1.2 Reply context

A reply-aware command receives typed context: selected account, source chat, invoking message identity, replied-to message identity, and validated metadata. Resolve the reply only inside the explicitly selected account. Reject missing, stale, mismatched, or cross-account reply references with normalized errors. Never trust caller-supplied sender, chat type, visibility, or permission claims; resolve trusted metadata and reauthorize through Core.

### 1.3 Confirmed /save

The owner invokes /save as a reply to a message:
1. Resolve the replied-to message in the selected account.
2. Retrieve its content through the standard adapter/read path.
3. Transfer content through Media Pipeline as needed.
4. Send it to that same account's Saved Messages (chat with self).
5. Preserve captions, filenames, and supported contextual metadata where the platform supports them.
6. Return a correlated result and audit the operation without copying message bodies or credentials into audit.

The destination write requires the selected account and Write permission. Retrieval must obey applicable account/client Read policy as well as Write for the destination. If the owner-command principal uses a distinct policy, define it explicitly rather than bypassing Policy Engine. No direct TDLib shortcut is allowed.

Errors distinguish at least: missing reply, unresolved reply, unavailable source, unsupported content, download failure, upload failure, destination denial, rate limit, timeout, and outcome unknown. Do not report success on an upload acknowledgement alone if a later terminal result exists.

Acceptance covers text/media, reply resolution, same-account routing, captions/filenames, unsupported/unavailable source, download/upload errors, permission denial, rate limit, duplicate invocation, timeout, terminal send result, audit secrecy, and account isolation. Synthetic fixtures only until compliance is resolved.

## 2. Media pipeline

### 2.1 Separation of concerns

Media transfer is separate from optional media interpretation. The pipeline provides metadata resolution and authorization, streaming download, private account-scoped staging, validation/integrity checks, storage adapter read/write, streaming upload, retention/cleanup, progress, cancellation, and normalized terminal results.

No interface or plugin accesses TDLib file paths directly. Filesystem paths are server-generated; caller-provided paths and URLs are never trusted.

### 2.2 Transfer contract

- Support streaming/chunked transfer; avoid loading large media fully into memory.
- Support resumability only where source and destination semantics make it safe; persist opaque transfer ID, offsets/checkpoints, expected size, and checksum.
- Validate declared and observed size, MIME type, file signature where practical, filename length/encoding, and content type. MIME and filenames are untrusted claims.
- Compute SHA-256 or stronger checksums and verify after staging and, where possible, after upload.
- Enforce configurable per-file, per-account, aggregate storage, concurrent-transfer, and temporary-disk quotas. These are resource safeguards, not arbitrary product-level batch caps.
- Deduplicate by checksum only when configured storage policy permits; never merge across accounts in a way that violates isolation.
- Use private temporary directories, restrictive permissions, atomic finalization, and cleanup on success, failure, cancellation, timeout, and startup recovery.
- Do not follow symlinks or caller-controlled paths. Prevent path traversal and archive/decompression bombs.
- Retention is configurable, including explicitly configured permanent retention where permitted. Expiry applies to active storage; backups/snapshots may retain prior copies.
- Preserve captions, filenames, and supported metadata for round-trip operations.
- Report progress and partial outcomes without exposing local paths or secrets.
- Reauthorize before upload/send and before returning media bytes to a client. Revocation during download prevents delivery.
- Never retry upload/send automatically after an unknown mutation outcome. Reconcile before retry.

### 2.3 Media processing

Transcription, OCR, image understanding, translation, compression, thumbnailing, and other transformations are optional processors invoked through explicit tasks. They do not run automatically on every incoming file. Processor input is a scoped media reference, not an unrestricted filesystem path. Outputs carry provenance, provider/model metadata where appropriate, retention, and sanitized audit metadata.

Real Telegram media must not be sent to AI/ML providers until the compliance gate is formally resolved and the owner approves the exact data flow.

## 3. Provider-independent AI task adapters

### 3.1 Task contract

Each AI task defines:
- Stable task name/version and narrow purpose.
- Explicit invocation source and authenticated principal.
- Exact input schema and maximum input size.
- Explicit account/chat/message/media references selected by caller.
- Required permission checks and data minimization.
- Provider-independent task interface.
- Provider capability declaration and selection/fallback policy.
- Output schema and validation.
- Timeout, cancellation, bounded retry, token/cost budget, concurrency, and rate-limit policy.
- Provenance (provider/model/version), usage metadata, and sanitized audit event.
- Input/output retention and deletion behavior.

Provider adapters implement a common interface; task logic does not depend on one vendor SDK. Support local and remote providers behind configuration, but no provider is enabled for real Telegram data while the compliance gate is closed.

### 3.2 Invocation and isolation

- No broad background analysis of chats or messages.
- No automatic AI invocation from message arrival, content, attachment, or inferred intent.
- No arbitrary prompt execution from Telegram content.
- Only explicitly selected input may be sent to a provider; no hidden chat history, neighboring messages, other accounts, or unrelated attachments.
- Re-check client/account/chat authorization before input resolution and before output delivery or a follow-up Telegram write.
- Treat input and model output as untrusted. Validate structured output; it cannot grant permissions or execute arbitrary actions.
- AI-generated Telegram writes are separate operations and require Write permission through normal Core authorization.
- Until compliance is resolved, implement/test only with synthetic fixtures and fake/local providers. The real-data path fails closed; a feature flag is not a compliance decision.

### 3.3 Utility catalog policy

Do not invent a confirmed AI utility catalog. Candidate tasks include voice transcription, OCR, translation, transcript cleanup, summarization of explicitly selected content, and text-to-speech. Keep them Candidate until owner confirmation. Implement generic provider/task interfaces and synthetic acceptance tests meanwhile.

## 4. Plugin and extension contract

### 4.1 Initial trust model

- Start with first-party, trusted, version-controlled modules.
- Arbitrary third-party in-process code is not allowed by default.
- Plugins cannot import/reach TDLib clients, database credentials, raw secrets, filesystem paths, or unrestricted network access.
- Plugins use versioned Gateway Core service interfaces and receive only declared capability-scoped handles.
- Every plugin action is subject to the same Policy Engine, audit, rate limits, and compliance gate as built-in actions.

### 4.2 Manifest

A manifest declares:
- Plugin ID, display name, publisher/owner, semantic version.
- Compatible Gateway API and plugin SDK ranges.
- Entry point and declared extension points.
- Configuration schema and secret references, never literal secrets.
- Requested capabilities: read, write, admin, media, events, scheduling, AI task invocation.
- Account/client enablement scope and resource budgets.
- Health check, startup/shutdown hooks, migration needs, and audit event namespace.
- Integrity/signature metadata for packaged releases.

Reject unknown capabilities, incompatible API versions, duplicate IDs, undeclared settings, and unsafe migration requests.

### 4.3 Lifecycle and controls

Required lifecycle: discovered → validated → installed → configured → enabled → healthy/degraded → disabled → uninstalled. Preserve audit history and configuration provenance. Enable/disable is owner-controlled and reversible. A failed startup or health check disables/quarantines the plugin without taking down Gateway Core.

Plugins have bounded execution time, memory/CPU where enforceable, concurrency, queue depth, and output size. All actions have correlation IDs and normalized errors. Plugin logs cannot contain message bodies, credentials, or local paths.

### 4.4 Isolation roadmap

First-party modules may initially run in-process only behind narrow service interfaces and explicit capability grants. Before loading third-party code, define and test a real sandbox/process boundary, signed package provenance, dependency policy, resource limits, network/filesystem denial, upgrade/rollback, and incident response. An in-process permission wrapper is not a sandbox.

## 5. Candidate modules, not approved requirements

Research-derived candidates, not a mandate to implement/enable without owner selection:
- AFK/status automation and scheduled status rotation.
- Keyword/regex auto-replies with priorities, cooldowns, allow/block lists.
- Reminders and recurring messages.
- Download manager and queue.
- Voice transcription, OCR, translation, transcript cleanup, and TTS.
- Explicitly selected chat summaries or exports.
- Formatting/text modes, profile automation, reactions, and channel utilities.
- Workflow templates and per-chat automation.

Shared foundations above should support these modules. Do not add autonomous background AI analysis or message-content command execution.

## 6. Acceptance criteria

- Commands: explicit invocation, registry validation, reply resolution, account isolation, permission denial, duplicate handling, timeout/cancel, normalized errors, and audit.
- /save: every case in §1.3, synthetic only.
- Media: streaming, quotas, checksum mismatch, malformed MIME, traversal/symlink, cancellation, restart cleanup, retention, duplicates, revocation during transfer, unknown upload result.
- AI: provider contract conformance, exact-input-only, explicit invocation, no background invocation, output validation, budgets/timeouts/cancel, provider failure, revocation, synthetic-only enforcement.
- Plugins: manifest validation, compatibility, capability denial, disabled/degraded states, failure isolation, resource limits, audit, and no Core/Policy bypass.
- Candidate modules remain visibly unapproved until owner confirms their user-facing behavior.

### Wave update (2026-11-07): AI contracts and first-party plugins

- AI task contract: implemented as provider-independent infrastructure only. `src/tg_control/ai.py` defines strict task input/output models, provider capability declarations, explicit provider selection, budgets (input size and estimated cost), timeouts, cancellation, conservative retry rules, output validation, usage reporting and sanitized audit. The application registers no task; `GET /v1/ai/tasks` is empty and every unapproved utility remains a Candidate. Telegram-derived input is rejected structurally and no configuration flag can enable it.
- Plugin manifest and registry: implemented for first-party modules. `src/tg_control/plugins.py` validates manifests (semver, known extension points and capabilities, budgets, `$secret:` references, no admin capability), requires explicit owner grants and a bound module before enable, executes through Core under one delegated client, and contains failures with timeouts, per-minute rate limits, failure counters and quarantine.
- Not implemented or not approved: real AI providers or utilities, cost accounting storage, remote plugin distribution/signing, marketplace, process-level plugin sandboxing, MCP/CLI/UI exposure of these surfaces, live Telegram data and deployment. No live integration has been verified.
