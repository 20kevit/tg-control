# Release process

A release is a *reviewed, reproducible artifact plus evidence*. It is not a tag,
a pushed image or a deployment. Nothing in this document authorizes publishing an
image, creating a GitHub release, or deploying to any host: those steps require
explicit owner approval each time.

## 1. Version and build metadata

| Field | Source | Where it appears |
|---|---|---|
| `version` | `src/tg_control/version.py::__version__` (single source of truth) | wheel metadata, `GET /v1/admin/diagnostics` → `build.version`, `release-provenance.json` |
| `schema_revision` | `src/tg_control/db.py::EXPECTED_SCHEMA_REVISION` (`0007`) | diagnostics, backup manifest, provenance |
| `source_revision` | `git rev-parse HEAD` at build time, injected as `TG_BUILD_REVISION` | provenance, diagnostics `build.source_revision` |
| `build_time` | `TG_BUILD_TIME` injected at build time | provenance, diagnostics `build.build_time` |
| `dirty` | `git status --porcelain` at build time | provenance (a dirty tree must not become a release) |

The runtime never consults Git: an installed wheel reports the version it was
built with, and diagnostics expose only these five non-sensitive values (no
paths, no hostnames, no credentials).

## 2. Reproducible candidate build

```bash
python scripts/build_release.py --dry-run          # provenance only, writes nothing
python scripts/build_release.py --output-dir dist  # builds wheel + SHA256SUMS
```

Outputs:

- `dist/tg_control-<version>-py3-none-any.whl` (dependency-free wheel; the
  runtime dependencies are pinned by `requirements.lock`),
- `dist/SHA256SUMS`,
- `dist/release-provenance.json` (version, schema revision, source revision,
  dirty flag, Python version, per-artifact SHA-256, `publish: false`).

Reproducibility rules:

- The wheel build uses pip's isolated build environment (setuptools/wheel
  downloaded per build); these are build-time tools, not shipped dependencies.
  Byte-identical rebuilds are **not** proven — the provenance record pins the
  source revision and hashes the produced artifact instead.

- `requirements.lock` is hash-pinned and installed with `--require-hashes`; the
  image and CI install from it, not from floating ranges.
- The container image is `python:3.11-slim-bookworm` (patch tag). Digest pinning
  is an **open item**: it needs registry access and owner approval.
- `--publish` is rejected by design (exit code 2) so automation cannot publish
  accidentally.

## 3. Release checklist

Run every item; record the command and the real output. An item that cannot run
in this environment is recorded as **Blocked**, never skipped silently.

- [ ] Working tree clean (`git status --porcelain` empty) and on the intended
      branch; `main` is never merged into a feature branch.
- [ ] `bash scripts/ci.sh` green (lint, `pip check`, contract parity, traceability
      coverage, full test suite).
- [ ] `scripts/gen_contracts.py` produces no diff; `scripts/check_interfaces.py`
      reports parity.
- [ ] `scripts/gen_traceability.py` produces no diff and every row carries an
      evidence level.
- [ ] `python scripts/build_release.py --output-dir dist` succeeds; tree was clean
      before the build; `release-provenance.json` has `dirty: false` and the
      expected `source_revision`/`schema_revision`.
- [ ] Migration state: new database reaches `head`; an existing previous-schema
      database upgrades with data preserved (`tests/test_migrations.py`).
- [ ] Disaster-recovery drill executed on a disposable pair
      (`tests/test_disaster_recovery.py`); archive verified and restored.
- [ ] Image built and the container smoke executed (`scripts/container_smoke.sh`)
      **on a host with a Docker daemon**; otherwise **Blocked**.
- [ ] `docker compose config` validates and contains no credential values.
- [ ] Vulnerability scan of the image and Python dependencies executed; results
      reviewed. `pip-audit`/`trivy` are unavailable in the current environment →
      **Blocked** until network/registry approval.
- [ ] Hosted CI run green on the release commit (`.github/workflows/ci.yaml`).
      No run has happened yet → **Not executed**.
- [ ] Documentation matches the artifact: `DEPLOYMENT.md`, `OPERATIONS.md`,
      `SECURITY.md`, `STATUS.md`, `ACCEPTANCE.md`, `DATA_MODEL.md`,
      `CONTRACT_INDEX.md`, `TRACEABILITY.md`.
- [ ] Owner approval recorded for: publishing, deployment, real Telegram data,
      compliance-gate changes, external service changes.

## 4. Artifact naming and provenance

| Artifact | Name | Notes |
|---|---|---|
| Wheel | `tg_control-<version>-py3-none-any.whl` | contains `tg_control` package only; no secrets |
| Checksums | `SHA256SUMS` | one line per artifact, `sha256  name` |
| Provenance | `release-provenance.json` | source revision, schema revision, dirty flag, Python version |
| Container image | `tg-control:<version>` (local tag `tg-control:local` in Compose) | not pushed anywhere |

A provenance record is evidence of *what was built*, not proof that the build was
reviewed. Keep it with the artifact and compare `source_revision` with the commit
that passed CI.

## 5. Compatibility policy

| Surface | Policy | Enforcement |
|---|---|---|
| HTTP `/v1` | additive changes only within a major version; breaking change requires `/v2` | `src/tg_control/contracts.py` registry + `tests/test_contracts.py` parity gate |
| MCP tools | same registry; a renamed tool is a breaking change | `scripts/check_interfaces.py` |
| CLI | same registry; flags/paths are part of the contract | `scripts/check_interfaces.py` |
| Database schema | monotonic Alembic revisions; every revision has a tested upgrade; downgrades only where tested | `EXPECTED_SCHEMA_REVISION` + startup gate + `tests/test_migrations.py` |
| Backup format | manifest version bumps on incompatible layout changes; v1 remains readable with a warning | `_parse_manifest` + `verify_archive` tests |
| Configuration | required variables may be added; removal/rename is breaking and documented in `DEPLOYMENT.md` | `missing required configuration` error contract |

Application and database versions are a pair: the gateway refuses to serve a
schema whose revision is not exactly the one it was built for, in either
direction.

## 6. Rollback decision points

Rollback is a decision, not a reflex. Use the first row that applies.

| Situation | Action | Evidence required |
|---|---|---|
| New image fails startup with a configuration error | fix configuration; no rollback needed | startup log line |
| New image fails because the schema is behind | run the documented migration | `startup.schema_rejected` state `behind` |
| New image fails because the schema is ahead | deploy the newer image; **do not downgrade the database blindly** | state `ahead` |
| New image starts but a migration is needed to make it work | the application never auto-migrates; run `migrate` explicitly, then restart | migration plan |
| Behavior regression, schema unchanged | redeploy the previous image tag | previous provenance record |
| Behavior regression, schema **upgraded** during the bad release | downgrade only if that downgrade is tested (`0007 → 0006` on a disposable database); otherwise restore the pre-release backup into a new target | downgrade test or restore drill |
| Data corruption suspected | stop the gateway, preserve the current database for inspection, restore a verified archive into a **new** target, validate, then switch | backup verification + restore drill |
| Unknown/foreign schema revision | refuse; investigate origin; never hand-edit `alembic_version` | diagnostics `migration_state: unknown` |

Rolling back the application does **not** roll back the database. Rolling back
the database does not roll back the application. State both explicitly in any
incident note.

## 7. Approval gates

The following require explicit owner approval and are out of scope for automated
work: publishing an image or package; creating a GitHub release or tag; deploying
to any host; exposing a port beyond loopback; enabling real Telegram
authentication; changing the compliance gate; changing DNS, firewall, GitHub
visibility or repository permissions; creating secrets or registries.

Until those approvals exist, the deliverable is a locally verified candidate
with the blocked items recorded in `docs/STATUS.md` and `docs/ACCEPTANCE.md`.
