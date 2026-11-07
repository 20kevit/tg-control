# Product requirements

TG-Control is a personal-account Telegram gateway, not a Bot API bot. Its clients are independently authenticated AI harnesses. A separately authenticated owner configures accounts and policies. The intended gateway does not autonomously interpret or act on message contents.

## Required behavior and acceptance

| ID | Requirement | Acceptance evidence |
|---|---|---|
| P1 | Multiple isolated accounts with stable ID, alias, and protected phone metadata | Two synthetic accounts have independent data; attempts to cross account boundaries are denied. |
| P2 | Multiple client identities and independent credentials | A client credential is issued once; rotation invalidates the old one; revocation prevents subsequent requests. |
| P3 | Account and chat authorization | Every action names an account; denied accounts and chats never return data or produce effects. |
| P4 | Independent Read, Write, Admin with independent scopes and exclusions | Matrix tests prove no permission implies another, exclusions override every grant, and private chats are denied unless explicitly scoped. |
| P5 | Discovery, history, search, metadata, media | Search/list results are filtered by authorized chat, including across accounts; history reports its sync/completeness limitations. |
| P6 | Send/edit/delete/forward/media and Telegram administrative actions | Synthetic operations execute only with the appropriate permissions; forwarded sources cannot bypass Read scope. |
| P7 | Batch workflows | Every target, including forward sources, is authorized before *any* effect; results are per-item and execution is not falsely atomic. |
| P8 | Retry and lifecycle | Only definitely rejected retryable operations are retried after the indicated interval; deadlines, disconnect and intervention are visible. |
| P9 | Shared core for HTTP, MCP, CLI and admin | Interface and negative tests exercise the same core; no interface calls an adapter directly. |
| P10 | Audit, retention, security | Decisions/outcomes are recorded without content or credentials, protected by owner access; retention defaults to 90 days. |
| P11 | Portability and recovery | PostgreSQL schema migrations, container deployment, backup and restore instructions are verified as far as the local environment allows. |
| P12 | Legal gate | Only synthetic data is used; live adapter remains unavailable until qualified advice/permission and explicit owner approval. |

## Non-goals / constraints

No promise of unlimited history, media availability, atomic cross-chat writes, unrestricted exports, a rule engine, message-triggered automation, Telegram Bot API, or production deployment. A production release requires the explicit approvals and evidence in `COMPLIANCE.md` and `ROADMAP.md`.


## Detailed contract references

The complete requirement map and autonomy/approval policy are in [CONTRACT_INDEX.md](CONTRACT_INDEX.md). Event delivery, durable scheduling, and workflows are specified in [EVENTS_JOBS_WORKFLOWS.md](EVENTS_JOBS_WORKFLOWS.md). Reply-aware commands including /save, media transfer, provider-independent AI tasks, and plugin lifecycle are specified in [COMMANDS_MEDIA_AI_PLUGINS.md](COMMANDS_MEDIA_AI_PLUGINS.md). These contracts are required foundations; optional user-facing modules remain candidates until owner-approved.
