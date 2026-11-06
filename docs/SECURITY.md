# Security and authorization model

## Principals and scopes

There are two trust roles: an owner-management principal and independent client principals. A client has no access to any account without an explicit account assignment. Owner management access is not a Telegram permission and must not be exposed as an AI tool. Each account assignment has separate Read, Write and Admin scope. A scope grants explicit chat IDs and/or selected chat types; exclusions take precedence. Read may explicitly be configured to include public groups/channels, but private groups and direct messages require an explicit chat ID. The secure initial default is deny all. New public chats are only included if a public type grant is configured; new private chats are never automatically included.

| Request | Required authorization |
|---|---|
| account/chat listing, history, search, message/media read | account assignment + Read; filter each chat |
| send/edit/delete/send media | account assignment + Write for target chat |
| forward | Read for source and Write for destination in the same account |
| Telegram administrative action | account assignment + Admin for target chat |
| manage accounts, clients, credentials, policies, settings and audit | owner-management credential only |

Never trust client-supplied account membership, chat type, identity or permissions. Every cross-account batch is fully pre-authorized before side effects. No bypass via HTTP, CLI, MCP or panel is allowed. Rotation and revocation are checked on subsequent requests rather than cached indefinitely.

## Threats and controls

- Credential theft/replay: random per-client secrets, stored as digests, TLS for any approved remote exposure; revoke/rotate. A bearer token remains replayable if stolen; TLS, short exposure and isolation mitigate but do not eliminate replay. Do not claim cryptographic replay protection.
- Prompt injection / stored XSS: content is opaque and untrusted; no interpretation/execution, escape output, UI uses text nodes and CSP. AI consumers must also treat content as untrusted.
- IDOR and cross-account leakage: resolve IDs within the selected account, deny by default, filter discovery/search, test multi-account and forward-source cases.
- Privilege escalation: Telegram Admin is not a gateway owner role and is independent of Read/Write. Owner routes reject client credentials.
- Browser attacks: verify Origin and allowed Host, no cookie authentication, no credential in URL, safe browser rendering, no permissive CORS. Local listeners bind loopback by default.
- Audit/log leakage: record metadata and result codes, never message bodies, tokens, phone numbers, filenames or raw exception messages. Application and reverse-proxy access logs must not capture request bodies or Authorization.
- Storage theft: protect volumes and backups with authenticated encryption and separate keys; encrypt sensitive metadata; per-account TDLib paths are UUID-derived. Do not commit secrets/session state. Synthetic account files use independently derived keys and restrictive paths; source-state persistence is optional. Cache entries are in memory, bounded, reauthorized before use and invalidated on mutation. Active-state expiry does not erase backups or storage snapshots.
- Failure: authorization and audit storage failures fail closed before effects. After an effect, a lost response or audit failure creates an unknown outcome: do not blindly repeat a non-idempotent operation.

## Operational boundaries

Only synthetic/test data is allowed until the compliance gate and owner approval are resolved. Remote access requires HTTPS, restricted ingress, Origin/Host validation, and authentication on every request; static bearer credentials do not provide OAuth or eliminate replay. Do not expose a development instance to the public internet. See `SECURITY_REVIEW.md` for the internal review, residual risks and release blockers, and `STATUS.md` for verification evidence.
