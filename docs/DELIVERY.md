# Delivery checkpoint — PostgreSQL metadata milestone, 2026-11-06

## Identity
Base: f9b3fa6776a1951430b55603266d68d42e2b18b8.
Branch: arena/9088bfcd-tg-control (fixed session branch; documented deviation from
arena-agent-2). Final SHA: `git rev-parse HEAD` after this report's commit.
No other agent work inspected/copied; no merge. **Product remains incomplete.**

## Implemented at this milestone
- PostgreSQL account/client metadata, independent scoped/excluded policy storage,
  credential lifecycle and sanitized metadata views, settings and durable audit.
- Explicit additive migration, transaction/checksum tracking, advisory lock and
  packaged SQL resource. No automatic connection-time schema modification.
- Salted scrypt digests only; revocation/rotation across connections; disabled
  clients blocked; concurrent rotation has one winner with consistent lock order.
- Gateway integration with stored policy and durable audit intent before effects.
  Audit intent failure blocks effects; final persistence failure returns an
  explicit unknown-outcome error. No automatic uncertain-write retries.
- Bounded request fields/media, finite/bounded rate-limit values, normalized
  adapter failures without exception payload leakage.
- Dependency constraints, test workflow, packaged schema and ADR 002.

Previous features retained: synthetic multi-account adapter, read/search/send,
reply-aware `/save`, batch preflight/per-item results, explicit bounded synthetic
AI task contract and fixture CLI. No live data or provider integration is enabled.

## Tests/checks actually executed
- `.venv/bin/pip install -e '.[test]'`: passed.
- `.venv/bin/python -m unittest discover -s tests -v`: **35 passed**, no skips.
  Includes 11 real-PostgreSQL integration tests in a disposable temporary cluster:
  migrations/checksum, reconnect persistence, credentials/revocation, client disable,
  saved-message policy/audit, parameter binding/rollback, retention/settings, outage,
  policy validation, simultaneous rotation, Unix-only listener and safe views
  (some tests exercise multiple categories).
- `.venv/bin/pip check`: passed, no broken requirements.
- `.venv/bin/pip wheel --no-deps --wheel-dir /tmp/tg-control-wheel .`: passed.
- Wheel SQL-resource inspection, compile check, CLI demo, constrained install and
  final diff check are recorded in the milestone command output.
- Earlier checkpoint: 19 tests and fixture CLI passed; system pip rejected by PEP
  668, resolved using a venv without overriding system protections.
- Docker not installed; no container build performed. GitHub workflow added but
  hosted CI run not yet verified. pip check is NOT a vulnerability audit.

## Architecture and security review
See ADR 002. PostgreSQL-only persistent path; no Redis, SQLite or live Telegram.
All SQL values are bound parameters; no raw message bodies or credentials in audit.
Only non-secret credential IDs returned by metadata views. Database errors are
normalized without exposing DSNs. Explicit statement/connect/lock timeouts.
Tests start a fresh bundled PostgreSQL over Unix sockets, verify no IP listener,
and delete only that disposable fixture database. No owner infrastructure used.

Remaining security boundaries: repository management methods are trusted local
owner primitives, not remote APIs. A client string is not authentication. No
service should expose the Gateway directly without a per-request identity layer.
Database roles/TLS/protected backups still need operational review. No production
readiness or comprehensive vulnerability/security approval is claimed.
Revocation cannot recall an already executing operation. Accepted audit without
completion means uncertain outcome; there is no distributed transaction with
Telegram. Settings for cache/media retention are stored but not yet implemented
by a real media pipeline. Fixture media is in-memory and size-limited, not streamed.

## Remaining stage work
0: detailed requirement matrix and expanded documents still needed.
1: current official terms research blocked by outbound host allowlist; compliance
   gate remains CLOSED pending qualified owner decision.
2: native TDLib JSON binding/build/runtime isolation and session-state proof missing.
3: metadata persistence now tested; production service configuration, account state
   paths and owner bootstrap/lifecycle integration still needed.
4/5: tested synthetic slices; edit/delete/forward/Admin operations missing.
6: authenticated API, full CLI, MCP, Web Admin and management boundaries missing.
7: Docker build, vulnerability audit, backup/restore rehearsal and hosted CI review.

Next safe milestone: authenticated local API shared by CLI/MCP/Web Admin, using
stored credentials/policy/audit. Native adapter can be built/tested without login.
No legal approval is needed for further synthetic engineering. Live login/content,
production exposure, owner migrations and deployment still require their gates.
