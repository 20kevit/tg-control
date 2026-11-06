# Execution status

Updated 2026-11-06. Branch: `arena/663cd534-tg-control`. Repository baseline contained only `README.md` and `docs/ARENA.md`; history at checkout is a single grafted initial commit. Both were read completely.

## Stage 0/1 baseline in progress

- Added product, architecture, policy/threat model, data model, API/ADR, compliance/TDLib assessment, roadmap and test strategy. Implemented capabilities: none yet; no login or real data used.
- Official Telegram terms reviewed. API §1.5 and content licensing AI restrictions are an unresolved release blocker; participant-specific consent is not equivalent to established permission. Native TDLib reference-page fetches returned HTTP 403; indexed official excerpts were accessible. Need actual native feasibility evidence without login.
- Initial environment: Python 3.11; Docker, PostgreSQL server, and TDLib not found on PATH. Python dependencies not preinstalled. PostgreSQL integration requires a safe isolated local server or container; no production infrastructure touched.
- Tests: not yet executed; project initially had no implementation or test suite.
- Next: establish test environment, implement and verify synthetic adapter, then gateway/policy/storage.

## Stages 0–2: baseline and offline adapter (2026-11-06)

- Stage 0 acceptance met: baseline docs and reproducible isolated Python test setup added. Stage 1 compliance sources and restrictions recorded; native TDLib runtime feasibility remains unverified. Stage 2 partial: normalized adapter protocol, isolated synthetic accounts/chats/messages/media, lifecycle states, injected failures and offline TDJSON client/request correlation with UUID-derived paths. No native library, network call or login.
- Files: `pyproject.toml`, `src/tg_control/adapters/`, `tests/test_adapter.py`, baseline docs.
- Verification: `.venv/bin/pytest -q` — 6 passed; `.venv/bin/ruff check src tests` — passed. The initial editable install failed because `src/` was not yet created; creating it and repeating succeeded. `pip check` passed.
- Security: synthetic fixtures only; TDJSON correlation rejects cross-client response completion and account path traversal. The actual TDLib binding and content-protection behavior are not implemented or verified.
- Next: metadata storage/migrations, authentication, server-side policy, and audit foundation.

## Stage 3 foundation, part 1 (2026-11-06)

- Added SQLAlchemy models, versioned Alembic initial migration, encrypted-phone storage field, secret-file configuration, high-entropy independently revocable credential digests, rotation, and independent deny-by-default Read/Write/Admin scope evaluation with exclusions. Added metadata-only audit model and retention helper. Owner management and Gateway Core are next; this is not yet an accessible API.
- Files: `src/tg_control/{config,db,auth,policy,audit}.py`, `alembic/`, `alembic.ini`, `tests/test_policy_auth.py`, `tests/test_migrations.py`.
- Verification: `.venv/bin/pytest -q` — 13 passed, including migrations and credential persistence on a temporary Unix-socket PostgreSQL instance supplied by `pgserver`; `.venv/bin/ruff check src tests alembic` — passed. One benign `XDG_RUNTIME_DIR` fallback warning. The first PostgreSQL test run failed during cleanup due to a mistaken `close()` call; fixed to the library's `cleanup()` and reran successfully.
- Security: credentials are random, stored only as SHA-256 digests, rotation serializes on the PostgreSQL client row; private type-wide grants are rejected and exclusions override explicit chat grants. Actual encryption is wired into account creation in the next stage. This is not yet a complete security review.
- Next: shared gateway execution, full pre-authorization and safe retry, then authenticated HTTP/management routes.

## Gateway core, synthetic read/write/admin (2026-11-06)

- Added discriminated action validation, server-side account/chat and forward-source authorization, filtering for search/discovery, whole-batch pre-authorization, per-item non-atomic execution, one-time persistent mutation keys, retry/deadline/reconnect handling, and metadata-only decisions/outcomes. Added migration `0002_execution_keys`. These are core functions; HTTP/MCP/CLI/UI are still pending.
- Files: `src/tg_control/{core,schemas,db}.py`, `src/tg_control/adapters/base.py`, `alembic/versions/0002_execution_keys.py`, `tests/test_core.py`, `docs/API.md`.
- Verification: `.venv/bin/pytest -q` — 23 passed, including synthetic send/edit/delete/forward/media/admin, cross-account batch rejection, retry delay/timeout, reconnect/reauth, credential and scope revocation during retries, replay rejection, audit failure and unknown mutation outcomes. `.venv/bin/ruff check src tests alembic` — passed. Temporary PostgreSQL migrations still pass.
- Security: one-time keys mitigate replay of mutation requests, but bearer-token theft remains a risk; never expose without an approved TLS boundary. Search rechecks each chat after retry delays. No Telegram adapter or real content used.
- Outstanding: authenticated API and owner management, retention enforcement, clients/interfaces, native TDLib, complete audit/ops/security review. Next: HTTP API and management.

## Synthetic HTTP vertical slice (2026-11-06)

- Added authenticated API, owner-only account/client/credential/policy/retention/audit management, synthetic session status/reconnect, encrypted phone metadata, pre-effect request auditing, request/decision outcomes, explicit Origin and Host checks, bounded request bodies, sanitized errors and security headers. Owner and AI credentials are non-interchangeable. Full synthetic read and write stages 3–5 have exercised API/core acceptance, but native Telegram support remains blocked/incomplete.
- Files: `src/tg_control/{api,security,request_audit,core,db,audit}.py`, `alembic/versions/0003_audit_subject.py`, `tests/test_api.py`, `tests/test_postgres_api.py`, `tests/test_core.py`, `docs/API.md`.
- Verification: `.venv/bin/pytest -q` — 32 passed, including an end-to-end API flow on isolated PostgreSQL and SQLite, cross-account search/isolation, auth rotation/revocation, private scope/exclusion, batch preflight, replay, Host/Origin, chunked body limit and audit secrecy. `.venv/bin/ruff check src tests alembic` — passed. One benign `XDG_RUNTIME_DIR` warning.
- Limitations: audit retention requires explicit owner-triggered pruning; media/cache settings are persisted but not enforced and the simulator loses messages/media on restart. No real TDLib client, native integration, live operation, or legal approval exists. Interfaces, retention and operational verification remain. Next: implement MCP/CLI/panel through the same core/API, then harden retention and operations.

## Synthetic interfaces (2026-11-06)

- Implemented official MCP SDK Streamable HTTP (`/mcp`) and a local stdio executable, CLI, and owner web panel. All tools use Gateway Core or its HTTP API; remote MCP authenticates every request and inherits Host/Origin protection. The panel manages accounts, client credentials, independent policies, session status/reconnect, saved retention and audit; client credentials appear once. It polls synthetic session status while open.
- Files: `src/tg_control/{mcp_server,mcp_stdio,cli,http_client,api,request_audit}.py`, `src/tg_control/static/`, `tests/test_mcp.py`, `tests/test_stdio_process.py`, `tests/test_cli.py`, `tests/test_browser.py`, `tests/browser/admin.cjs`, `package*.json`, `docs/{MCP,CLI,ADMIN}.md`.
- Verification: official SDK Streamable HTTP and stdio clients, actual stdio subprocess and loopback gateway, CLI allow/deny, and a real headless Chromium owner workflow with an XSS payload all passed in targeted tests. Browser test initially failed because the minimal environment lacked NSS libraries; the test now extracts the bundled compatible libraries. Initial stdio test used a non-file stderr object, fixed to a temporary file and reran. Full verification: `.venv/bin/pytest -q` — 40 passed, one benign `XDG_RUNTIME_DIR` warning; `.venv/bin/ruff check src tests alembic` and `npm run check` — passed.
- Limitations: remote MCP uses static Bearer authentication, not OAuth; ChatGPT remote connector compatibility is unverified. No real Telegram, native TDLib, automatic audit pruning, persistent synthetic content, or enforced cache/media retention yet. Stage 6 remains open pending final interface/security review. Next: retention, deployment and operational hardening.

## Synthetic retention and persistence (2026-11-06)

- Added optional encrypted per-account synthetic state (`TG_SYNTHETIC_STATE_DIR`), with UUID-derived paths/keys, atomic writes, restrictive permissions and a POSIX single-writer lock. Synthetic media age-based expiry is enforced on access, startup and approximately hourly; `null` disables age expiry. It cannot resurrect expired bytes, and files/backups may retain historical copies. Without the state directory, simulator data is ephemeral.
- Added bounded in-memory read-result caching with configurable TTL, per-request authorization, mutation invalidation and in-flight generation protection. Cache holds no media bytes. Added owner-visible persistence status and destructive-retention warning; fixed a UI issue that could hide a one-time credential if refresh failed.
- Files: `src/tg_control/{cache,config,core,api}.py`, `src/tg_control/adapters/{synthetic,synthetic_store,base}.py`, `src/tg_control/static/`, `tests/test_retention.py`, `tests/browser/admin.cjs`, updated storage/security/API docs.
- Verification: targeted retention tests exercise encrypted isolation, restart, key mismatch, POSIX lock, permissions/path rejection, media expiration/permanent mode, failed writes, disabled-account cleanup/audit, TTL, policy revocation and in-flight invalidation. Real Chromium verifies one-time credential visibility even when refresh fails. Full verification: `.venv/bin/pytest -q` — 48 passed with one benign `XDG_RUNTIME_DIR` warning; `.venv/bin/ruff check src tests alembic` and `npm run check` — passed. The first HTTP persistence test used the wrong test-helper signature; fixed and reran successfully.
- Limitations: single worker/instance only; caches are process-local. Cleanup is not guaranteed while offline and can lag up to an hour while running. Audit pruning remains explicit. No native TDLib, real Telegram or production deployment. Next: containers, backup/restore and security/operations review.

## Security and authorization hardening (2026-11-06)

- Added final delivery-time Read-scope checks across batch results, including trusted chat re-resolution and per-chat filtering/audit for discovery and wildcard search. Access revoked during an adapter lookup, an in-flight read, or a later batch action no longer delivers the affected earlier read. Already-executing mutations cannot be retroactively canceled.
- Added metadata-only audit for requests rejected at the Host/Origin/body boundary; MCP credential rotation/revocation and allowed/denied Origin/Host paths are covered. Secret files are now bounded and must be private, including Alembic's database URL file. The browser owner session locks after 15 minutes idle, clears sensitive rendered UI state, and rejects late responses from a previous session. Actual CLI and MCP subprocesses are exercised against a loopback gateway.
- Files: `src/tg_control/{core,api,config,request_audit}.py`, `src/tg_control/static/app.js`, `alembic/env.py`, relevant core/API/MCP/CLI/browser tests, `docs/{API,ADMIN}.md`.
- Verification: full working-tree suite `.venv/bin/pytest -q` — 60 passed, one optional native probe skipped and one benign `XDG_RUNTIME_DIR` warning; `.venv/bin/ruff check src tests alembic scripts` and `npm run check` passed. This full run also includes the in-progress backup/operations tests. No real account/data used.
- Remaining: independent security audit, rate limiting/MFA and remote OAuth, native adapter and protected-content rules, tested container deployment, automated monitoring, and unresolved legal/owner gates.

## Backups, dependency lock and local deployment artifacts (2026-11-06)

- Implemented an explicit offline backup/restore command using AES-256-GCM authenticated encryption, private temporary libpq credentials, opt-in private synthetic state, path validation, no-overwrite behavior and an explicit empty-target confirmation. The original gateway key and backup key must be preserved separately. Restore is transactional for PostgreSQL but not atomic across the database and files; plaintext temporary dumps/archives require a private tmpfs and careful cleanup.
- Added a local-only Compose/Dockerfile configuration, non-overwriting synthetic secret initialization, hash-locked Python runtime/build dependencies, and operations, backup, recovery and internal security-review documentation. Docker/Compose are not installed in this environment, so image builds and actual container startup are **unverified**; YAML and local-only security settings were checked statically. Image digests/OS scanning remain open.
- Files: `src/tg_control/backup.py`, `scripts/init_synthetic_secrets.py`, `Dockerfile`, `compose*.yaml`, `.dockerignore`, `requirements*.{in,lock}`, `tests/{test_backup,test_operational_config}.py`, `docs/{OPERATIONS,SECURITY_REVIEW}.md`, updated project metadata/docs.
- Verification: isolated PostgreSQL backup/restore (both direct and actual CLI subprocess) passed, including wrong-key, tampering, held state lock, pre-existing target, non-empty database and authenticated path-traversal negative cases; restored credentials, policy, audit and media were checked. Synthetic secret initialization refuses overwrite and avoids stdout disclosure. The complete working-tree suite last passed 60 tests with one optional native test skipped and one benign environment warning; Ruff and JS syntax checks passed. Clean hash-locked installation, built-wheel static assets and three command entry points, and `pip check` passed. `pip-audit` on the lock and `npm audit` reported no known vulnerabilities at review time.
- Outstanding: Docker runtime/image validation, operational automation/alerts, OAuth/MFA/rate-limiting, native adapter and compliance/owner approvals. An offline native TDLib build/probe is in progress; no parameters, login or network access to Telegram are permitted.

## Offline native TDJSON feasibility (2026-11-06)

- Built the official TDLib source at commit `4c6377aa7386c39217878a9a56d581922ad37ca4` as `libtdjson.so.1.8.69` on Linux x86-64. CMake was initially unavailable and the first configure failed for missing gperf; installed CMake in an ignored environment, built gperf from a pinned mirror and generated matching OpenSSL headers, then the build succeeded. Sources, compiler details, hashes and reproducibility limits are documented in `docs/TDLIB_OFFLINE.md`. The 1.5 GiB build and 142 MiB library are temporary ignored artifacts, not part of Git.
- Added an opt-in subprocess test that installs and verifies a Linux seccomp network-syscall deny filter **before** loading TDLib, runs a synchronous synthetic parsing function, creates two empty native clients, verifies distinct client/request correlation and `authorizationStateWaitTdlibParameters`, then closes both. No TDLib parameters, credentials, phone numbers, login, network access or real content were supplied. Seccomp is not a complete native-code sandbox.
- Verification: `TG_TDLIB_LIBRARY=... .venv/bin/pytest -q` — 61 passed, one benign `XDG_RUNTIME_DIR` warning; `.venv/bin/ruff check src tests alembic scripts` and `npm run check` — passed.
- This is a feasibility probe only. A native/live TDLib adapter, authorization/synchronization, protected content, official sponsored-message support and real-account flows remain unimplemented. Compliance and owner decisions remain blocking. Docker startup, remote OAuth, independent security review and production readiness remain unverified.

## Synthetic owner notification and acceptance review (2026-11-06)

- Replaced transient session warnings with a persistent in-panel alert for synthetic sessions requiring intervention, disconnected sessions, and failed status checks. Alerts survive view changes and ordinary success notices, clear when checks recover, and are discarded on logout; a rejected owner credential locks and clears the workspace. This remains a browser-open synthetic poll, not durable or push monitoring.
- Added an explicit implementation/evidence/outstanding acceptance matrix, and marked the compliance research and offline TDLib proof-of-concept stages separately from the still-unresolved real-data gate and unimplemented native adapter. No release or live capability is claimed.
- Files: `src/tg_control/static/{index.html,app.js,style.css}`, `tests/browser/admin.cjs`, `docs/{ADMIN,ACCEPTANCE,ROADMAP,SECURITY_REVIEW}.md`.
- Verification: Chromium exercised persistent intervention, disconnect, failed check, recovery, invalid-credential lock and old-response logout behavior. Full `.venv/bin/pytest -q` with the network-blocked native probe enabled — 61 passed, one benign `XDG_RUNTIME_DIR` warning; `.venv/bin/ruff check src tests alembic scripts` and `npm run check` — passed. Large generated build/dependency scratch was moved out of the repository and is not committed.
- Remaining: real adapter, remote OAuth/ChatGPT compatibility, push/always-on alerts, Docker runtime verification, independent security review and release/production gates.
