# TG-Control

A personal Telegram AI gateway for multiple accounts and separately authorized AI clients. This is **not** a Telegram Bot API product.

> **Compliance gate: unresolved.** This repository may only use synthetic/test data. Do not log into real Telegram accounts, ingest or send real Telegram content to AI, deploy to production, or use real credentials without the separate decisions and owner approvals in [the compliance register](docs/COMPLIANCE.md). No compliance claim is made.

The project is under active implementation. See [status](docs/STATUS.md) for implemented features, actual test results and known gaps; [roadmap](docs/ROADMAP.md) for acceptance criteria; [product requirements](docs/PRD.md), [architecture](docs/ARCHITECTURE.md), [security](docs/SECURITY.md) and [security review](docs/SECURITY_REVIEW.md), [data model](docs/DATA_MODEL.md), [API](docs/API.md), [development](docs/DEVELOPMENT.md), [testing](docs/TESTING.md), [operations and backup](docs/OPERATIONS.md), [offline TDLib feasibility](docs/TDLIB_OFFLINE.md), and [execution contract](docs/ARENA.md) for the authoritative baseline. Never commit real secrets, sessions or content.


## Authoritative contracts

Start with [the product and engineering contract index](docs/CONTRACT_INDEX.md). Detailed contracts cover [events, durable jobs and workflows](docs/EVENTS_JOBS_WORKFLOWS.md) and [commands, media, AI tasks and plugins](docs/COMMANDS_MEDIA_AI_PLUGINS.md). Arena must follow these contracts while respecting the unresolved [compliance gate](docs/COMPLIANCE.md).
