# Competitor review — Telegram MCP Server

**Audited:** 2026-11-08  
**Competitor repository:** https://github.com/mcp-telegram/mcp-telegram  
**Competitor documentation:** https://github.com/mcp-telegram/mcp-telegram/blob/main/README.md  
**TG-Control baseline:** synthetic feature expansion on this working branch, checked against `docs/CONTRACT_INDEX.md`, `docs/API.md`, `docs/MCP.md`, `docs/STATUS.md`, `docs/ROADMAP.md`, `docs/TDLIB_OFFLINE.md`, `src/tg_control/contracts.py` and `src/tg_control/adapters/*`.

> **Purpose:** Inventory the competitor's published capabilities and compare them with TG-Control's synthetic feature and capability catalog. The owner approved synthetic fixture/schema coverage and descriptive capability discovery; that approval does not authorize live Telegram integration or imply every competitor behavior is a production requirement.

## 1. Executive summary

Telegram MCP Server is a close direct competitor: it operates a personal Telegram account through MTProto/GramJS and exposes Telegram operations as MCP tools. Its README currently advertises **182 tools** and extensive account, messaging, group, profile, story, business and Telegram-specific functionality.

TG-Control has a broader gateway/control-plane foundation (separate clients, account-scoped Read/Write/Admin policy, HTTP/CLI/MCP, audit, durable events/jobs/workflows, plugin registry, observability and operations). Its synthetic feature catalog contains 147 operation rows (12 base operations and 135 feature actions); 124 feature actions have synthetic handlers. The Telegram adapter is **not live**: offline TDLib work is an ABI/feasibility probe and unwired boundary models, not a working Telegram login, synchronization or operation adapter.

Therefore, the status labels below distinguish:
- **Implemented — synthetic:** a fixture handler exists; no live behavior is implied.
- **Partial — synthetic:** only part of a grouped capability is represented, or a shared gateway foundation exists without live Telegram support.
- **Platform-gated — synthetic:** a fixture handler exists, but the simulator reports eligibility separately and defaults eligibility flags to false.
- **Not implemented / live blocked:** no usable synthetic handler or live Telegram adapter exists.
- **Compliance blocked / owner approval required:** execution fails closed until its separate gate is resolved.
- **Not applicable / different approach:** TG-Control intentionally uses a different interface or architecture.

Every row in this matrix is synthetic coverage only. Every operation row that is returned reports `live_status: blocked_compliance`; client discovery is filtered by current permission and actual accessible chats, while the owner view and [published catalogue](../contracts/capability_catalog.json) retain the full inventory. Capability counts or fixtures must not be read as live support. The client response exposes no chat IDs/titles/content and fails closed if chat metadata is unavailable.

## 2. Important baseline facts

### Telegram MCP Server
- Personal-account userbot using MTProto through GramJS (not the Bot API).
- MCP server with a large set of individually discoverable tools.
- QR-based login, persistent session, multiple independent session paths, and a shared daemon mode for multiple clients using one account session.
- Supports stdio MCP; README documents client setup for Claude, Cursor, VS Code and Mastra.
- Supports npx/npm, binaries and Docker; documents SOCKS5/MTProxy and WSS transport options.
- README groups the tools into Auth, Messaging, Scheduled, Reading, Drafts, Forum Topics, Polls, Reactions, Paid Reactions, Audio Transcription, Fact-check, Stickers, Media, Groups, Chat Info, Folders, Global Privacy, Invite Links, Contacts, Moderation, Profiles, Account, Pinning, Chat Settings, Admin Toggles, Stats, Inline Bots & Buttons, Real-Time Polling, Stories, Discussion, Read Receipts, Boosts, Business and opt-in features.

### TG-Control
- Current MCP surface is intentionally compact: `tg_accounts`, `tg_capabilities`, and `tg_execute`, plus client-facing jobs/events/commands/AI operations in the shared contract registry. It is not a one-MCP-tool-per-Telegram-method design.
- The synthetic execution contract covers the 12 base operations plus 135 strict typed feature actions (124 synthetic handlers; 11 explicitly classified without handlers or gated). Client discovery through `GET /v1/accounts/{account_id}/capabilities`, MCP `tg_capabilities`, and CLI `capabilities` is filtered by current per-operation permissions and accessible-chat scopes; missing metadata fails closed. The owner-only Admin capability panel retains the full catalogue, and execution remains under shared Core authorization. All 147 rows and requirement/action/handler/test traceability are published in `docs/contracts/capability_catalog.json` and `docs/FEATURE_TRACEABILITY.md`.
- Multiple account metadata, per-client account grants, independent Read/Write/Admin permissions, chat scopes/exclusions and server-side authorization are implemented in the synthetic gateway.
- Durable event subscriptions, jobs/scheduling, manual workflows, command routing, /save, media staging, audit and first-party plugin foundations exist in synthetic form.
- No live TDLib adapter, real login, live updates/history/media, or real Telegram end-to-end behavior is implemented.
- Remote MCP currently uses configured static Bearer credentials; OAuth discovery/dynamic registration and ChatGPT remote connector interoperability are not demonstrated.
- The external Telegram AI-data compliance gate remains unresolved. Do not connect live Telegram-derived data to AI clients or providers until the legal/compliance and owner gates are resolved.

## 3. Feature-by-feature comparison (synthetic coverage)

Rows summarize fixture/schema coverage, not live implementation or product parity. The operation-level sources of truth are [FEATURE_CAPABILITIES.md](../FEATURE_CAPABILITIES.md), the [147-entry catalogue](../contracts/capability_catalog.json), and [FEATURE_TRACEABILITY.md](../FEATURE_TRACEABILITY.md). Every returned row has `live_status: blocked_compliance`. Platform eligibility is synthetic metadata only. Financial or compliance-sensitive operations remain disabled even when their schemas appear in the catalogue.

### A. Account connection, sessions and client access

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| Personal Telegram account over MTProto | Live adapter blocked | Offline TDLib boundary only; no live authorization, synchronization or Telegram operation adapter. Compliance and explicit owner gates remain unresolved. |
| QR login and 2FA flow | Not implemented / live blocked | No authorization or 2FA flow is implemented. |
| Login / logout / connection status tools | Partial — synthetic | Synthetic session status, reconnect handling and monitoring exist; no real Telegram login/logout. |
| Persistent Telegram session across restarts | Not implemented for Telegram | Synthetic persistence is not a Telegram authorization session. |
| Multiple Telegram accounts | Partial — synthetic | Isolated account model and per-client grants exist; no real Telegram accounts are connected. |
| Many MCP clients sharing one live account connection | Partial — shared gateway | Shared Core and client identities exist; there is no live Telegram connection or daemon. |
| Per-account session path and isolation | Partial — offline boundary | Policy and synthetic state isolation exist; real TDLib session/database lifecycle is not integrated. |
| Session list / terminate another session | Implemented — synthetic | Typed session-list and termination fixtures are available; they do not manage real Telegram sessions. |
| QR login entirely through an MCP client | Not implemented / live blocked | No real authorization flow is exposed through MCP. |

### B. Messaging, reading, search and drafts

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| List chats / dialogs | Implemented — synthetic | Generic `list_chats`; no live Telegram data. |
| Read message history with pagination | Implemented — synthetic | History contract exists; live completeness and Telegram paging are unverified. |
| Get a single message | Implemented — synthetic | Generic `get_message`; no live data. |
| Search messages within a chat | Implemented — synthetic | Generic `search`, with Read-scope enforcement. |
| Global search across authorized accounts/chats | Partial — synthetic | Search is account-wide or chat-scoped; cross-account searches require explicit authorized per-account actions. |
| Search chats | Not implemented | No distinct Telegram-native chat search operation; message search is separate. |
| Send / edit / delete messages | Implemented — synthetic | Generic message mutations; no live writes. |
| Forward messages | Implemented — synthetic | Source Read and destination Write are both enforced; no live forwarding. |
| Reply/quote formatting and Premium message effects | Implemented — synthetic | Reply, quote text and effect identifiers are typed; real Premium eligibility and Telegram behavior are unverified. |
| Get message permalink | Implemented — synthetic | Fixture permalink lookup; no live link resolution. |
| Translate a message using Telegram translation | Compliance blocked (disabled) | Always fails closed with `compliance_blocked`; no configuration bypass while compliance is unresolved. |
| Send typing indicator | Implemented — synthetic | Bounded transient fixture only. |
| Get unread count / mark as read | Implemented — synthetic | Unread counts and explicit read markers are modeled. |
| Unread mentions / unread reactions | Implemented — synthetic | Synthetic unread counters include mentions and reactions. |
| Get replies / thread replies | Implemented — synthetic | Typed reply lookup is available for fixture messages. |
| Saved dialogs / Saved Messages discovery | Partial — synthetic | Saved Messages and `/save` are modeled; no live Saved Messages access. |
| Save / list / clear message drafts | Implemented — synthetic | Draft CRUD fixtures use scoped chat actions. |
| Native scheduled-message list / delete | Implemented — synthetic | Native scheduled-message fixture list/delete exists; separate durable gateway jobs do not make it live Telegram scheduling. |

### C. Media, files and rich message types

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| Send media/file | Implemented — synthetic | Generic `send_media`; staging/signature checks and fixtures only. |
| Download media | Partial — synthetic | Synthetic `get_media` and private chunk staging exist; no live download adapter or client streaming route. |
| Profile photo retrieval | Implemented — synthetic | Profile-photo retrieval/expiry fixtures only. |
| Web page preview retrieval | Implemented — synthetic | Web-preview fixture lookup only. |
| Send voice message | Implemented — synthetic | Typed voice media fixture with bounded signature checks. |
| Send round video / video note | Implemented — synthetic | Typed video-note fixture with bounded signature checks. |
| Send photo/video album as grouped media | Implemented — synthetic | Bounded album fixture; results are explicitly non-atomic. |
| Send location / live location | Implemented — synthetic | Validated location and live-location fixture messages. |
| Send venue / contact / dice | Implemented — synthetic | Typed venue/contact/dice fixture payloads. |
| Resumable media transfer | Required foundation — not implemented | Internal chunk staging exists; client-facing byte routes, resumable offsets and durable transfer checkpoints remain absent (ADR-015). |
| File size, MIME, checksum, quota and retention controls | Implemented — synthetic | Synthetic staging validates size, MIME/signature and SHA-256, enforces private quota/TTL controls; no live transport/storage parity. |

### D. Reactions, polls, stickers and inline interactions

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| Send / read message reactions | Implemented — synthetic | Reaction write/read fixtures; no live updates. |
| Set default reaction / recent and top reactions | Implemented — synthetic | Default-reaction and reaction-summary fixture operations. |
| Paid reactions and privacy controls | Partial — synthetic; paid mode disabled | Ordinary reaction privacy is fixture-only. Paid reactions always fail with `approval_required`; Premium eligibility cannot bypass this financial-action gate. |
| Create polls | Implemented — synthetic | Poll fixtures validate unique options and bounds. |
| Vote / retrieve results / voters / close poll | Implemented — synthetic | Synthetic vote/results/voter counters and close operation. |
| Search / browse / send sticker sets | Implemented — synthetic | Sticker set browse/send fixtures only. |
| Inline bot query and send result | Implemented — synthetic | Inline query/result fixtures; no live bot calls. |
| Read message buttons / press callback buttons | Implemented — synthetic | Button and callback fixtures; no live callback execution. |
| Telegram Mini App / Web App interaction | Implemented — synthetic | Web App metadata fixture only; no real web-app launch or signed data exchange. |

### E. Groups, channels, topics and moderation

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| Get chat info / member list / own role | Implemented — synthetic | Typed chat info, member list and role fixtures; no live Telegram reads. |
| Create / edit groups | Implemented — synthetic | Synthetic group creation and chat-title mutation; no live group lifecycle. |
| Join / leave chat | Implemented — synthetic | Join/leave fixture operations; no real account membership changes. |
| Invite users | Implemented — synthetic | Admin-scoped fixture operation; connected Telegram rights are not exercised. |
| Kick / ban / unban members | Implemented — synthetic | Admin-scoped ban/unban/member-status fixtures. |
| Set / remove admin | Implemented — synthetic | Admin role fixtures; no Telegram-side rights changes. |
| Set chat permissions / slow mode | Implemented — synthetic | Validated permission and slow-mode fixtures. |
| Retrieve admin log | Implemented — synthetic | Scoped synthetic admin-log read. |
| Invite-link create / list / revoke | Implemented — synthetic | Admin-scoped invite-link lifecycle fixtures. |
| Forum topic list / read / create / edit / delete | Implemented — synthetic | Typed topic CRUD/list fixtures. |
| Per-topic unread counts | Implemented — synthetic | Synthetic topic unread metadata. |
| Pin / unpin messages | Implemented — synthetic | Admin-scoped message pin/unpin fixtures. |
| Mute / archive / pin chat / mark dialog unread | Implemented — synthetic | Synthetic chat-state fixture mutations. |
| Channel signatures / anti-spam / forum mode / prehistory toggles | Implemented — synthetic | Typed channel-setting fixtures; real account rights and platform versions are unverified. |
| Approve join requests | Implemented — synthetic | Admin-scoped join-request fixtures. |
| Configure allowed chat reactions | Implemented — synthetic | Admin-scoped allowed-reaction fixture. |
| Chat folders: create / edit / delete / reorder / tags / suggestions | Partial — synthetic | Folder CRUD/reorder and scope filtering are implemented; folder suggestions remain unsupported. |

### F. Contacts, profile, privacy and account settings

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| List / add contacts | Implemented — synthetic | Contact discovery and add/remove fixtures. |
| Contact requests | Implemented — synthetic | Contact-request list/accept fixtures. |
| Block / unblock user / report spam | Implemented — synthetic | Block/unblock and Admin-scoped report fixtures. |
| Read / update profile | Implemented — synthetic | Profile read/update fixtures. |
| Profile photo set / delete | Implemented — synthetic | Profile-photo set/delete fixtures with validation. |
| Emoji status / profile color / birthday / personal channel | Implemented — synthetic | Typed profile-detail fixtures; Premium/platform eligibility is not verified live. |
| Per-category privacy settings | Implemented — synthetic | Typed privacy-setting fixtures. |
| Global privacy settings | Implemented — synthetic | Global account-level privacy fixtures. |
| Auto-delete timer | Implemented — synthetic | Chat-scoped auto-delete timer fixture operations. |

### G. Stories, discussion, read receipts and analytics

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| Read stories (all / peer / by ID) | Implemented — synthetic | Story list/read fixtures, subject to synthetic visibility rules. |
| Story view statistics | Implemented — synthetic | Synthetic viewer/statistics fixtures. |
| Publish / edit / delete stories | Implemented — synthetic | Story create/update/delete fixtures. |
| React / pin / archive / stealth mode / report stories | Partial — synthetic | React/pin/archive/report fixtures exist; stealth mode is unsupported and Premium eligibility is not verified. |
| Discussion group information for channel posts | Implemented — synthetic | Discussion lookup hides linked chats outside the caller's current Read scope. |
| Groups eligible for channel discussion | Implemented — synthetic | Synthetic eligible-chat fixture list, filtered by current scope. |
| Read participants / outbox read date | Not implemented | `get_read_participants` and `get_outbox_read_date` have schemas but no synthetic handlers. |
| Channel / supergroup statistics | Implemented — synthetic | Admin/eligibility checks are modeled with synthetic stats fixtures. |
| Boost status / boosters list | Platform-gated — synthetic | Fixture handlers exist; synthetic account eligibility defaults false. No live boost status. |

### H. Telegram Business, Stars, group calls and quick replies

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| Telegram Business chat links CRUD / resolve | Platform-gated — synthetic | Business-link fixtures require the synthetic `business` eligibility flag; no live API. |
| Business hours / location / greeting / away / intro messages | Platform-gated — synthetic | Settings fixtures require synthetic Business eligibility; platform/version coverage is unverified. |
| Stars balance / transactions / top-up options | Platform-gated — synthetic | Read-only synthetic Stars fixtures require eligibility; no live balance or payment operation. |
| Stars subscriptions management | Partial — synthetic; approval required | Subscription listing is synthetic; subscription mutation remains disabled with `approval_required`. |
| Star Gifts browse / save / convert | Partial — synthetic; conversion disabled | Browse/save fixtures are available when eligible; financial/irreversible gift conversion requires separate owner approval. |
| Group call info / participant list | Platform-gated — synthetic | Fixture metadata requires synthetic `group_calls` eligibility; no live call data. |
| Quick replies / quick-reply messages | Implemented — synthetic | Quick-reply CRUD/send fixtures only. |
| Audio transcription via Telegram Premium | Not implemented / eligibility-gated | No synthetic transcription handler; Premium flag alone does not enable execution. |
| Fact-check get / edit / delete | Not implemented / eligibility-gated | Schemas and eligibility metadata exist; no synthetic fact-check handlers. |

### I. Real-time updates and event delivery

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| Telegram update polling using state cursors | Not implemented for live Telegram | Durable synthetic event subscriptions/cursors exist; there is no live TDLib update source. |
| Channel update polling | Not implemented for live Telegram | No live channel-update ingestion; synthetic event source only. |
| Durable event subscriptions, ack/nack, replay and dead letters | Implemented — synthetic | Gateway-level event delivery is durable and scoped; live adapter event ingestion is absent. |
| Per-client event/chat filtering and authorization recheck | Implemented — synthetic | Filtering and delivery-time scope rechecks are tested with synthetic events only. |

### J. Developer experience, deployment and integration

| Competitor capability | TG-Control status | Notes / synthetic scope / blockers |
|---|---|---|
| MCP stdio | Implemented — synthetic | MCP stdio contract tested without live Telegram. |
| MCP Streamable HTTP | Implemented — synthetic | Static Bearer authentication; OAuth compatibility is not established. |
| Individual MCP tool for every Telegram method | Different approach | Compact `tg_accounts`, `tg_capabilities`, `tg_execute` plus service tools; 74 registry operations preserve interface parity without one tool per platform method. |
| CLI | Implemented — synthetic | Client CLI includes `accounts`, `capabilities` and shared `execute`; owner/client separation is preserved. |
| HTTP API / OpenAPI | Implemented — synthetic | Shared Gateway Core, 74 registered contracts and authenticated schema. |
| Admin web panel | Implemented — synthetic | Owner management surface now includes the operation-level capability panel; account data is synthetic. |
| Multiple AI clients with separate identities and scopes | Implemented — synthetic | Separate identities and account/chat scopes; no live Telegram data. |
| Docker packaging | Static checks locally; container hosted baseline only | The current local CI passes, but no container runtime is available in this sandbox; current-branch hosted/container verification remains pending. See STATUS.md. |
| Prebuilt binaries / npx one-command install | Not implemented | Packaging convenience, not core product behavior. |
| SOCKS5 / MTProxy / WSS transport configuration | Not implemented | No live Telegram transport is configured or exercised. |
| Shared daemon for concurrent local MCP clients | Partial — shared gateway | Shared gateway architecture exists; live TDLib lifecycle and concurrency remain unimplemented. |
| Remote MCP OAuth / dynamic client registration | Not implemented | Static Bearer only; OAuth discovery, dynamic registration and ChatGPT connector compatibility remain unverified. |
| Session and API secret file permissions | Partial — foundation | Credential controls and protected storage exist; live Telegram session custody/rotation is not implemented. |
| Proxy-aware deployment diagnostics | Not implemented | No transport-specific proxy diagnostics are implemented. |

## 4. TG-Control capabilities that are stronger or broader than this competitor's published scope

These are areas where TG-Control already has a distinct synthetic gateway architecture worth preserving:

- Per-client, per-account Read / Write / Admin grants with independent scopes and exclusions, enforced inside the gateway.
- Separate owner and client identities; credential rotation and revocation.
- One shared Gateway Core for HTTP, MCP, CLI and Web Admin.
- Batch preauthorization, per-item outcomes, idempotency and conservative unknown-outcome handling.
- Durable jobs, lifecycle management, retries, timezones, recovery and execution-time authorization.
- Durable events with subscriptions, cursors, ack/nack, replay, backpressure and dead-letter isolation.
- Workflow definitions, dry-run and bounded execution.
- First-party plugin registry with explicit capability grants and no policy bypass.
- Structured audit, redaction, metrics, traces, diagnostics, backup/restore and deployment checks.

These capabilities are currently verified in synthetic mode, not with a live Telegram account.

## 5. Risks, caveats and non-goals

1. **Synthetic is not live:** an implemented synthetic action is not a completed Telegram feature. The real TDLib adapter, login, sync, error mapping and live end-to-end tests remain absent.
2. **Do not clone tool count:** 182 advertised MCP tools does not mean 182 features are all equally useful. Preserve TG-Control's generic contract unless individual tool discoverability materially improves usability.
3. **Telegram API limitations:** some capabilities require Premium, business eligibility, admin rights, specific chat types or Telegram-side support. Arena must document and test actual limitations rather than promise universal access.
4. **Security:** administrative/profile/privacy/payment-related operations should remain behind explicit Admin policy and strong audit controls. Stars/top-ups/subscriptions can have financial effects and require a separate owner decision.
5. **Compliance gate:** Telegram's API Terms and Content Licensing restrictions on AI/ML use of Telegram data remain unresolved in TG-Control's compliance record. Competitor availability is not legal permission. No real Telegram data may be ingested into AI clients/providers until the gate is resolved.
6. **Scope:** approved synthetic fixtures and status reporting do not authorize live Telegram operations. Arena must not interpret a competitor feature as a live product commitment merely because it is documented or simulated here.

## 6. Sources and evidence

- Competitor README and complete published tool/category matrix: https://github.com/mcp-telegram/mcp-telegram/blob/main/README.md
- Competitor repository: https://github.com/mcp-telegram/mcp-telegram
- TG-Control contract registry: https://github.com/20kevit/tg-control/blob/main/src/tg_control/contracts.py
- TG-Control API contract: https://github.com/20kevit/tg-control/blob/main/docs/API.md
- TG-Control MCP contract: https://github.com/20kevit/tg-control/blob/main/docs/MCP.md
- TG-Control current status: https://github.com/20kevit/tg-control/blob/main/docs/STATUS.md
- TG-Control roadmap: https://github.com/20kevit/tg-control/blob/main/docs/ROADMAP.md
- TG-Control offline TDLib boundary: https://github.com/20kevit/tg-control/blob/main/docs/TDLIB_OFFLINE.md
- Telegram API Terms: https://core.telegram.org/api/terms
- Telegram Content Licensing: https://telegram.org/tos/content-licensing?setln=en

## 7. Owner decision log

**Decision status:** Synthetic-only schemas, fixtures and capability discovery are approved; live Telegram operations remain blocked.

The matrix no longer asks for per-row approval of synthetic fixtures. Any change that activates a live operation, transmits real data, performs a financial action, or changes the permission/compliance boundary still requires the applicable owner approval and compliance decision. Synthetic and live evidence must remain separately labeled.
