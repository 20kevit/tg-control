# Synthetic feature and capability catalogue

**Updated:** 2026-11-08
**Status:** Synthetic-only implementation and descriptive discovery. This catalogue is not evidence of live Telegram support.

TG-Control exposes operation-level capability metadata while preserving the shared Gateway Core and Policy Engine. The catalogue currently contains **147 rows**: 12 base operations and 135 typed feature actions. The synthetic adapter has fixture handlers for 124 of the feature actions. A registered schema or synthetic fixture never implies that a live TDLib adapter exists; all rows report live support as blocked by the unresolved compliance gate.

## Discovery interfaces

| Interface | Binding | Principal and purpose |
|---|---|---|
| HTTP | `GET /v1/accounts/{account_id}/capabilities` | Client credential; returns only the operations supported by that client's current permissions and accessible chat scopes. |
| MCP | `tg_capabilities` | Client-plane equivalent of the permission- and chat-filtered HTTP endpoint. |
| CLI | `capabilities --account-id <UUID>` | Client-plane equivalent of the permission- and chat-filtered HTTP endpoint. |
| Owner HTTP / Admin UI | `GET /v1/admin/accounts/{account_id}/capabilities` | Owner-only full account catalogue for the Admin panel; never exposed as an MCP tool. |

Client discovery returns no message content, chat IDs, titles or other chat metadata, and does not grant Read, Write or Admin access. It filters every row against the operation's declared permission, its actual accessible chat metadata, and any additional required permission (for example, `forward` needs both source Read and destination Write). Chat-scoped rows are omitted when no currently permitted chat is confirmed; absent, malformed, timed-out or failed chat metadata hides those rows (fail closed). The synthetic metadata hook does not reconnect. Core re-reads the client's current credential and independent scopes after the awaited lookups, before returning the filtered catalogue. No client response includes operation rows outside the caller's current grants.

Every action still executes through `actions.execute` / `POST /v1/execute` / `tg_execute` and centralized Core authorization. Exclusions override grants. Account-level outputs that contain chat IDs are independently filtered to authorized chats. Mutations are preauthorized as a batch, require a one-time idempotency key, and remain non-atomic after execution begins.

The client endpoint only reports accounts assigned to that client. The owner endpoint is separately authenticated and receives the full 147-entry account catalogue without changing client policy. Eligibility flags come only from the synthetic adapter profile; they are not fetched from or verified against Telegram.

## Status fields

`status` describes the current account/platform or policy gate. `adapter_support` independently describes whether the synthetic adapter has a handler. These dimensions can differ: for example, an operation may have a synthetic fixture handler but show `capability_unavailable` because the fixture account is not eligible. The Admin UI shows both badges. The top-level `adapter_mode` is `synthetic`, `live_adapter_enabled` is `false`, and `live_blocker` explicitly states that compliance is unresolved.

| Status | Meaning |
|---|---|
| `synthetic_fixture_supported` | The operation is present in the synthetic handler allowlist. This is fixture behavior only. |
| `capability_unavailable` | A declared platform/account eligibility flag is explicitly false. This is not a live Telegram check. |
| `eligibility_unknown` | Eligibility metadata was not supplied or is unknown; do not treat it as eligible. |
| `not_implemented_or_unverified` | No synthetic handler is registered, or the operation/version mapping is not verified. |
| `approval_required` | The adapter fails closed because a separate owner approval is required. |
| `compliance_blocked` | The adapter fails closed because the compliance gate is unresolved. |

`adapter_support` is `synthetic_fixture` only for allowlisted synthetic handlers; otherwise it is `not_implemented_or_version_unverified`. `account_eligible` is `true`, `false`, or `null` (unknown) when an operation declares a dependency. `conditional_blockers` maps parameter choices to fail-closed normalized errors (for example, `set_reaction` with `paid: true` is `approval_required`). Each row declares `permission`, `additional_permissions`, and `chat_required`; the latter identifies operations that need a currently allowed chat before the row is disclosed to a client. `live_status` is `blocked_compliance` for every row. `official_api_support` is a cautious, non-executable note and does not establish feature parity or Telegram-version compatibility.

## Operation groups

Feature action schemas and their required Core permissions are defined in `src/tg_control/features.py`. `params` is strict and operation-specific; unexpected fields are rejected. The current groups are:

- **Messaging:** chat metadata, replies/permalinks, unread markers, drafts, native scheduled-message fixtures, typing indicators, auto-delete timers, and translation status.
- **Media:** albums, voice/video-note fixtures, rich location/contact messages, profile-photo retrieval, and web-page preview fixtures.
- **Interactive:** polls, reactions, stickers, inline-bot results, and button/Web App fixtures.
- **Groups:** member and role discovery, invite links, moderation, topics, chat permissions/state, folders, channel settings, and statistics.
- **Contacts:** contact lists/requests and synthetic add/remove/block/report operations.
- **Account settings:** profile, privacy, reaction privacy, and synthetic session management.
- **Stories:** read/publish/update/delete/reaction/pinning/archive/discussion/statistics fixtures.
- **Business / Stars:** Business-link/settings, quick replies, Stars, gifts, subscriptions, and group-call metadata. Platform eligibility is not inferred from the presence of fixture state.

These synthetic operations are called only through the shared typed batch action schema. They do not create extra HTTP routes or independent MCP business logic. Interface bindings for discovery are registered in `src/tg_control/contracts.py` and checked against `docs/contracts/interfaces.json`.

## Disabled and incomplete operations

- `translate_message` is always refused with `compliance_blocked` while the compliance gate is unresolved; no configuration toggle overrides this.
- `convert_gift` and `manage_star_subscription` are refused with `approval_required` because they are financial or otherwise sensitive Telegram actions. They have no synthetic mutation handler.
- Paid reactions are refused with `approval_required` (`set_reaction` where `paid: true`); ordinary synthetic reactions remain available under Write scope. The Admin panel and capability metadata expose this conditional blocker.
- Other platform-dependent operations fail closed when the synthetic account's eligibility flag is false. Missing or unknown eligibility is not success.
- The exact 11 feature actions without synthetic handlers are listed with reasons in [FEATURE_TRACEABILITY.md](FEATURE_TRACEABILITY.md): `get_fact_check`, `translate_message`, `get_folder_suggestions`, `set_story_stealth`, `get_outbox_read_date`, `get_read_participants`, `convert_gift`, `manage_star_subscription`, `transcribe_audio`, `edit_fact_check`, and `delete_fact_check`. Translation remains compliance-blocked; gift conversion and subscription changes require separate owner approval; the remaining actions have no verified synthetic implementation (and platform-dependent ones also require eligibility).
- The complete generated **147-row** capability inventory, with support/eligibility/blocker/scope metadata, is [docs/contracts/capability_catalog.json](contracts/capability_catalog.json). The 135-row requirement → action → handler → test matrix is [FEATURE_TRACEABILITY.md](FEATURE_TRACEABILITY.md). Regenerate both with `.venv/bin/python scripts/gen_feature_catalog.py`; CI/source tests enforce the 12 + 135 and 124 + 11 counts.
- The standard base operations also remain synthetic; none has a live adapter binding.

Do not use these endpoints to log into Telegram, inspect real account eligibility, send or receive real content, connect a live account, or imply that compliance has been approved. Real Telegram authentication, synchronization, media transfer, writes, and updates remain disabled.

## Evidence

- Schemas, operation permissions and blocker metadata: `src/tg_control/features.py`
- Handler allowlist and fixtures: `src/tg_control/adapters/synthetic.py`
- Capability metadata: `src/tg_control/capabilities.py`; complete generated catalogue: `docs/contracts/capability_catalog.json`
- Requirement → action → handler → test matrix: `docs/FEATURE_TRACEABILITY.md`, generated by `scripts/gen_feature_catalog.py`
- Central authorization, preauthorization, client capability filtering and output filtering: `src/tg_control/core.py`
- HTTP/MCP/CLI contract registration: `src/tg_control/api.py`, `src/tg_control/mcp_server.py`, `src/tg_control/cli.py`, `src/tg_control/contracts.py`
- Admin renderer: `src/tg_control/static/index.html`, `src/tg_control/static/app.js`, `src/tg_control/static/style.css`
- Synthetic and authorization coverage: `tests/test_features.py`, `tests/test_api.py`, `tests/test_cli.py`, `tests/test_mcp.py`, `tests/browser/admin.cjs`, `tests/test_contracts.py`

Regenerate interface evidence with `python scripts/gen_contracts.py` and verify it with `python scripts/check_interfaces.py`. The capability count is derived at runtime, not hardcoded as an authorization decision.
