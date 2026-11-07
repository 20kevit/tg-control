# Development

Use Python and isolated, synthetic data. Never put secrets, real phone numbers, sessions, or Telegram data in the repository. Use an isolated environment and run the documented tests before committing. The project does not authorize login to real Telegram or infrastructure changes. As implementation lands, this guide will list exact setup, migration, formatting, and testing commands. See `COMPLIANCE.md`, `ROADMAP.md`, and `STATUS.md` for the current gate and evidence.

## Setup and checks

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
npm ci --ignore-scripts
.venv/bin/pytest -q
.venv/bin/ruff check src tests alembic scripts
npm run check
```

The synthetic adapter is in-process and offline; it never invokes TDLib. `tdlib_poc.py` models client correlation and account paths, not a native binding. An optional standalone offline native ABI probe is described in `TDLIB_OFFLINE.md`. Synthetic source data is ephemeral unless a private `TG_SYNTHETIC_STATE_DIR` is configured; with it, per-account encrypted data can survive restart. PostgreSQL metadata migrations and credential primitives are implemented and tested against an isolated local PostgreSQL instance. Live Telegram integration is not implemented.

The development test extra includes `pgserver`, which launches a temporary PostgreSQL instance on a Unix socket during the migration test; it is stopped and removed after the test. Never point tests at a real database.

Browser tests need Node.js 22.17+ on Linux x86-64 and use a locally installed, bundled Chromium through Playwright. They start only a temporary loopback server and use synthetic credentials/data. On other platforms, run supported system-browser tooling separately; no browser or node_modules is committed.

`requirements.lock` contains hash-pinned runtime and build dependencies for Python 3.11. Regenerate it after reviewing changes with `pip-tools==7.6.2` using the command in its header; install with `pip install --require-hashes -r requirements.lock`, then `pip install --no-deps --no-build-isolation .`. Audit the lock and npm dependencies regularly; see `SECURITY_REVIEW.md`.
