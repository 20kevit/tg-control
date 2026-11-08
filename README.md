# TG-Control

A personal Telegram AI gateway for multiple accounts and separately authorized AI clients. This is **not** a Telegram Bot API product.

> **Compliance gate: unresolved.** This repository may only use synthetic/test data. Do not log into real Telegram accounts, ingest or send real Telegram content to AI, deploy to production, or use real credentials without the separate decisions and owner approvals in [the compliance register](docs/COMPLIANCE.md). No compliance claim is made.

The project is under active implementation. See [status](docs/STATUS.md) for implemented features, actual test results and known gaps; [roadmap](docs/ROADMAP.md) for acceptance criteria; [product requirements](docs/PRD.md), [architecture](docs/ARCHITECTURE.md), [security](docs/SECURITY.md) and [security review](docs/SECURITY_REVIEW.md), [data model](docs/DATA_MODEL.md), [API](docs/API.md), [development](docs/DEVELOPMENT.md), [testing](docs/TESTING.md), [operations and backup](docs/OPERATIONS.md), [offline TDLib feasibility](docs/TDLIB_OFFLINE.md), [deployment](docs/DEPLOYMENT.md), [backup and disaster recovery](docs/BACKUP_RESTORE.md), [release process](docs/RELEASE_PROCESS.md), and [execution contract](docs/ARENA.md) for the authoritative baseline. Never commit real secrets, sessions or content.


## Authoritative contracts

Start with [the product and engineering contract index](docs/CONTRACT_INDEX.md). Detailed contracts cover [events, durable jobs and workflows](docs/EVENTS_JOBS_WORKFLOWS.md), [commands, media, AI tasks and plugins](docs/COMMANDS_MEDIA_AI_PLUGINS.md), and the [synthetic feature and capability catalogue](docs/FEATURE_CAPABILITIES.md). Arena must follow these contracts while respecting the unresolved [compliance gate](docs/COMPLIANCE.md).

## Container quickstart (synthetic only)

Use the [synthetic Docker setup](docs/DEPLOYMENT.md#3-configure) for prerequisites, private secret files, UID ownership, explicit migrations and startup. It must only be run against disposable local data.

The image and synthetic Compose smoke passed on a hosted runner at the revision recorded in [status](docs/STATUS.md); that does not establish a local sandbox, VPS, production or live Telegram deployment.

## Operations quickstart (synthetic only)

Illustrative commands; configure the private synthetic database and secrets first, and replace the placeholders. Migration writes to the configured database: use only a disposable target. Run the server and subsequent client commands in separate terminals.

```sh
python -m alembic upgrade head
uvicorn tg_control.api:app_factory --factory --host 127.0.0.1 --port 8000
.venv/bin/tg-control --token-file <owner-token-file> diagnose   # read-only diagnostics
.venv/bin/tg-control-backup verify --input <archive> --backup-key-file <key>
.venv/bin/tg-control-preflight --json   # read-only deployment preflight
```

Every error response uses the envelope documented in [API](docs/API.md); list endpoints use scope-bound cursors; health, metrics, logs, retention pruning, backup verification and graceful-shutdown behavior are documented in [operations](docs/OPERATIONS.md). All data is synthetic until the [compliance gate](docs/COMPLIANCE.md) is resolved.
