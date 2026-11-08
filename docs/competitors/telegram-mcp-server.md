# Competitor review — Telegram MCP Server

**Audited:** 2026-11-08  
**Competitor repository:** https://github.com/mcp-telegram/mcp-telegram  
**Competitor documentation:** https://github.com/mcp-telegram/mcp-telegram/blob/main/README.md  
**TG-Control baseline:** `main`, checked against `docs/CONTRACT_INDEX.md`, `docs/API.md`, `docs/MCP.md`, `docs/STATUS.md`, `docs/ROADMAP.md`, `docs/TDLIB_OFFLINE.md`, `src/tg_control/contracts.py` and `src/tg_control/adapters/*`.

> **Purpose:** Inventory the competitor's published capabilities, compare them with the current TG-Control implementation, and let the owner decide which missing product features should become approved requirements. This document is a selection backlog, not authorization for Arena to implement every listed feature.

## 1. Executive summary

Telegram MCP Server is a close direct competitor: it operates a personal Telegram account through MTProto/GramJS and exposes Telegram operations as MCP tools. Its README currently advertises **182 tools** and extensive account, messaging, group, profile, story, business and Telegram-specific functionality.

TG-Control has a broader gateway/control-plane foundation (separate clients, account-scoped Read/Write/Admin policy, HTTP/CLI/MCP, audit, durable events/jobs/workflows, plugin registry, observability and operations). However, its current Telegram adapter is **not live**: the implementation and verification baseline is synthetic-only. The offline TDLib work is an ABI/feasibility probe and unwired synthetic models, not a working Telegram login, synchronization or operation adapter.

Therefore, the status labels below distinguish:
- **Implemented — synthetic:** the corresponding generic gateway behavior exists and is tested against synthetic data; it is not available against a real Telegram account.
- **Partial / foundation:** some shared infrastructure exists, but the competitor's user-facing Telegram capability is not fully implemented.
- **Missing:** no corresponding Telegram feature is currently exposed/implemented in TG-Control.
- **Not applicable / different approach:** competitor-specific convenience or interface shape that TG-Control may not need.

No competitor feature should be marked complete in TG-Control solely because a generic contract, mock, TDLib model or test fixture exists.

## 2. Important baseline facts

### Telegram MCP Server
- Personal-account userbot using MTProto through GramJS (not the Bot API).
- MCP server with a large set of individually discoverable tools.
- QR-based login, persistent session, multiple independent session paths, and a shared daemon mode for multiple clients using one account session.
- Supports stdio MCP; README documents client setup for Claude, Cursor, VS Code and Mastra.
- Supports npx/npm, binaries and Docker; documents SOCKS5/MTProxy and WSS transport options.
- README groups the tools into Auth, Messaging, Scheduled, Reading, Drafts, Forum Topics, Polls, Reactions, Paid Reactions, Audio Transcription, Fact-check, Stickers, Media, Groups, Chat Info, Folders, Global Privacy, Invite Links, Contacts, Moderation, Profiles, Account, Pinning, Chat Settings, Admin Toggles, Stats, Inline Bots & Buttons, Real-Time Polling, Stories, Discussion, Read Receipts, Boosts, Business and opt-in features.

### TG-Control
- Current MCP surface is intentionally compact: `tg_accounts` and `tg_execute`, plus client-facing jobs/events/commands/AI operations in the shared contract registry. It is not a one-MCP-tool-per-Telegram-method design.
- The generic synthetic execution contract currently covers: list chats, search, history, get message, get media, send, edit, delete, forward, send media, set chat title and set member status.
- Multiple account metadata, per-client account grants, independent Read/Write/Admin permissions, chat scopes/exclusions and server-side authorization are implemented in the synthetic gateway.
- Durable event subscriptions, jobs/scheduling, manual workflows, command routing, /save, media staging, audit and first-party plugin foundations exist in synthetic form.
- No live TDLib adapter, real login, live updates/history/media, or real Telegram end-to-end behavior is implemented.
- Remote MCP currently uses configured static Bearer credentials; OAuth discovery/dynamic registration and ChatGPT remote connector interoperability are not demonstrated.
- The external Telegram AI-data compliance gate remains unresolved. Do not connect live Telegram-derived data to AI clients or providers until the legal/compliance and owner gates are resolved.

## 3. Feature-by-feature comparison and owner selection

For every row marked **Missing** or **Partial**, choose one:
- `[ ] Required` — approve it as product scope for TG-Control.
- `[ ] Not needed now` — explicitly defer it.
- `[ ] Revisit` — keep it as a candidate for a later decision.

Rows already implemented synthetically do not need a selection. They still require the live adapter and applicable acceptance evidence before they are real Telegram capabilities.

### A. Account connection, sessions and client access

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| Personal Telegram account over MTProto | Partial / blocked | TDLib boundary and offline probe only; no live adapter. Compliance gate applies. |
| QR login and 2FA flow | Missing | TG-Control has no real authorization flow. |
| Login / logout / connection status tools | Partial | Synthetic account status and reconnect monitoring exist; no real login/logout. |
| Persistent Telegram session across restarts | Missing for live Telegram | Synthetic persistence is not a Telegram authorization session. |
| Multiple Telegram accounts | Partial | Multi-account model and per-client account grants exist synthetically; no real account connections. |
| Many MCP clients sharing one live account connection | Partial | TG-Control has one gateway/core and client identities, but no live Telegram connection/daemon to share. |
| Per-account session path and isolation | Partial | Storage/policy boundaries exist; real TDLib per-account session/database lifecycle is not implemented. |
| Session list / terminate another session | Missing | Competitor exposes session management. |
| QR login entirely through an MCP client | Missing | Competitor documents this UX; not present in TG-Control. |
| **Owner decision for missing items** |  | QR/2FA: [ ] Required [ ] Not needed now [ ] Revisit · Session management: [ ] Required [ ] Not needed now [ ] Revisit |

### B. Messaging, reading, search and drafts

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| List chats / dialogs | Implemented — synthetic | Generic `list_chats`; no live Telegram data. |
| Read message history with pagination | Implemented — synthetic | History contract exists; live completeness/Telegram paging not verified. |
| Get a single message | Implemented — synthetic | Generic `get_message`. |
| Search messages within a chat | Implemented — synthetic | Generic `search` with scope enforcement. |
| Global search across authorized accounts/chats | Implemented — synthetic | Synthetic account-wide/cross-account search exists. |
| Search chats | Partial | Chat listing/search foundation exists; no separate Telegram-native chat-search parity claim. |
| Send / edit / delete messages | Implemented — synthetic | Generic operations; no live writes. |
| Forward messages | Implemented — synthetic | Source Read and destination Write authorization modeled. |
| Reply/quote formatting and Premium message effects | Partial / Missing | Reply-aware `/save` exists synthetically; competitor-specific quoteText/effect parameters are not a demonstrated feature. [ ] Required [ ] Not needed now [ ] Revisit |
| Get message permalink | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Translate a message using Telegram translation | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Send typing indicator | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Get unread count / mark as read | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Unread mentions / unread reactions | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Get replies / thread replies | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Saved dialogs / Saved Messages discovery | Partial | `/save` destination is modeled; full saved-dialog tools are not present. [ ] Required [ ] Not needed now [ ] Revisit |
| Save / list / clear message drafts | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Native scheduled-message list / delete | Partial | Durable jobs/scheduler foundations exist, but Telegram-native scheduled-message management is not implemented. [ ] Required [ ] Not needed now [ ] Revisit |

### C. Media, files and rich message types

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| Send media/file | Implemented — synthetic | Generic `send_media`; synthetic staging/validation only. |
| Download media | Partial | Synthetic `get_media` and private staging exist; no live download adapter or streaming client download route. |
| Profile photo retrieval | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Web page preview retrieval | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Send voice message | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Send round video / video note | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Send photo/video album as grouped media | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Send location / live location | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Send venue / contact / dice | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Resumable media transfer | Partial | Media lifecycle foundation exists; resumable transfers remain explicitly outstanding. [ ] Required [ ] Not needed now [ ] Revisit |
| File size, MIME, checksum, quota and retention controls | Partial / foundation | Synthetic staging has validation, checksum, quota and TTL; live transport/storage parity remains outstanding. |

### D. Reactions, polls, stickers and inline interactions

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| Send / read message reactions | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Set default reaction / recent and top reactions | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Paid reactions and privacy controls | Missing | Stars/Premium dependent. [ ] Required [ ] Not needed now [ ] Revisit |
| Create polls | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Vote / retrieve results / voters / close poll | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Search / browse / send sticker sets | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Inline bot query and send result | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Read message buttons / press callback buttons | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Telegram Mini App / Web App interaction | Missing | Not advertised in the primary README tool matrix as a general personal-account feature. [ ] Required [ ] Not needed now [ ] Revisit |

### E. Groups, channels, topics and moderation

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| Get chat info / member list / own role | Partial | Generic discovery and member-status admin primitives exist synthetically; detailed Telegram chat/member queries are absent. [ ] Required [ ] Not needed now [ ] Revisit |
| Create / edit groups | Partial | Generic `set_title` exists; full group lifecycle is absent. [ ] Required [ ] Not needed now [ ] Revisit |
| Join / leave chat | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Invite users | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Kick / ban / unban members | Partial | Generic `set_member_status` exists synthetically; Telegram-specific operations and rights are not implemented. |
| Set / remove admin | Partial | Same generic admin primitive only; full role and rights management is absent. [ ] Required [ ] Not needed now [ ] Revisit |
| Set chat permissions / slow mode | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Retrieve admin log | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Invite-link create / list / revoke | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Forum topic list / read / create / edit / delete | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Per-topic unread counts | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Pin / unpin messages | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Mute / archive / pin chat / mark dialog unread | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Channel signatures / anti-spam / forum mode / prehistory toggles | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Approve join requests | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Configure allowed chat reactions | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Chat folders: create / edit / delete / reorder / tags / suggestions | Missing | [ ] Required [ ] Not needed now [ ] Revisit |

### F. Contacts, profile, privacy and account settings

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| List / add contacts | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Contact requests | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Block / unblock user / report spam | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Read / update profile | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Profile photo set / delete | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Emoji status / profile color / birthday / personal channel | Missing | Some require Telegram Premium. [ ] Required [ ] Not needed now [ ] Revisit |
| Per-category privacy settings | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Global privacy settings | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Auto-delete timer | Missing | [ ] Required [ ] Not needed now [ ] Revisit |

### G. Stories, discussion, read receipts and analytics

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| Read stories (all / peer / by ID) | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Story view statistics | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Publish / edit / delete stories | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| React / pin / archive / stealth mode / report stories | Missing | Some are Premium dependent. [ ] Required [ ] Not needed now [ ] Revisit |
| Discussion group information for channel posts | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Groups eligible for channel discussion | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Read participants / outbox read date | Missing | Telegram visibility/size limitations apply. [ ] Required [ ] Not needed now [ ] Revisit |
| Channel / supergroup statistics | Missing | Telegram rights and account eligibility apply. [ ] Required [ ] Not needed now [ ] Revisit |
| Boost status / boosters list | Missing | [ ] Required [ ] Not needed now [ ] Revisit |

### H. Telegram Business, Stars, group calls and quick replies

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| Telegram Business chat links CRUD / resolve | Missing | [ ] Required [ ] Not needed now [ ] Revisit |
| Business hours / location / greeting / away / intro messages | Missing | Business-account eligibility may apply. [ ] Required [ ] Not needed now [ ] Revisit |
| Stars balance / transactions / top-up options | Missing | Optional env-gated competitor feature. [ ] Required [ ] Not needed now [ ] Revisit |
| Stars subscriptions management | Missing | Optional env-gated competitor feature. [ ] Required [ ] Not needed now [ ] Revisit |
| Star Gifts browse / save / convert | Missing | Optional env-gated competitor feature. [ ] Required [ ] Not needed now [ ] Revisit |
| Group call info / participant list | Missing | Optional env-gated competitor feature. [ ] Required [ ] Not needed now [ ] Revisit |
| Quick replies / quick-reply messages | Missing | Optional env-gated competitor feature. [ ] Required [ ] Not needed now [ ] Revisit |
| Audio transcription via Telegram Premium | Missing | Competitor offers a Premium-dependent transcription flow. [ ] Required [ ] Not needed now [ ] Revisit |
| Fact-check get / edit / delete | Missing | Availability and Telegram-side rights may be limited. [ ] Required [ ] Not needed now [ ] Revisit |

### I. Real-time updates and event delivery

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| Telegram update polling using state cursors | Missing for live Telegram | TG-Control has a durable synthetic event pipeline, but no live TDLib update source. |
| Channel update polling | Missing for live Telegram | [ ] Required [ ] Not needed now [ ] Revisit |
| Durable event subscriptions, ack/nack, replay and dead letters | Implemented — synthetic | Stronger gateway-level foundation exists; live adapter event ingestion is absent. |
| Per-client event/chat filtering and authorization recheck | Implemented — synthetic | Does not prove live Telegram update delivery. |

### J. Developer experience, deployment and integration

| Competitor capability | TG-Control status | Notes / owner decision |
|---|---|---|
| MCP stdio | Implemented — synthetic | Tested without live Telegram. |
| MCP Streamable HTTP | Implemented — synthetic | Static Bearer authentication; OAuth compatibility not established. |
| Individual MCP tool for every Telegram method | Different approach / partial | TG-Control exposes compact `tg_accounts` + `tg_execute`; do not expand tool count without a usability reason. |
| CLI | Implemented — synthetic | More extensive gateway CLI and owner/client separation. |
| HTTP API / OpenAPI | Implemented — synthetic | Shared Gateway Core and versioned contracts. |
| Admin web panel | Implemented — synthetic | Owner management surface exists; synthetic account data only. |
| Multiple AI clients with separate identities and scopes | Implemented — synthetic | TG-Control's policy model is more granular than the competitor's README-described single-session setup. |
| Docker packaging | Implemented / verified at recorded CI baseline | See exact revision and evidence in STATUS.md; later docs-only commit needs its own CI run. |
| Prebuilt binaries / npx one-command install | Missing | Packaging convenience, not core product behavior. [ ] Required [ ] Not needed now [ ] Revisit |
| SOCKS5 / MTProxy / WSS transport configuration | Missing | Telegram network transport feature; evaluate based on deployment needs. [ ] Required [ ] Not needed now [ ] Revisit |
| Shared daemon for concurrent local MCP clients | Partial | Gateway architecture is shared, but live TDLib lifecycle and concurrency behavior are not implemented. |
| Remote MCP OAuth / dynamic client registration | Missing | TG-Control explicitly documents this interoperability gap. [ ] Required [ ] Not needed now [ ] Revisit |
| Session and API secret file permissions | Partial / foundation | TG-Control has credential controls and protected storage; live Telegram session custody/rotation is not implemented. |
| Proxy-aware deployment diagnostics | Missing | [ ] Required [ ] Not needed now [ ] Revisit |

## 4. TG-Control capabilities that are stronger or broader than this competitor's published scope

These are not reasons to skip competitor features; they are areas where TG-Control already has a distinct architecture worth preserving:

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
6. **Scope:** checkboxes are owner decisions. Arena must not interpret a competitor feature as approved merely because it is documented here.

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

**Decision status:** Pending owner review.

After the owner marks the feature rows:
1. Convert only `Required` rows into approved product requirements and acceptance criteria.
2. Keep `Not needed now` rows deferred and visible; do not implement them.
3. Keep `Revisit` rows as candidates with no implementation commitment.
4. Arena may then update PRD, contract index, roadmap, acceptance and traceability, and implement approved features in synthetic mode first.
5. Any live Telegram integration remains blocked until the compliance and explicit owner gates are resolved.
