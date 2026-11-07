# Deployment

Scope: an owner-operated, **single-instance, synthetic-only** deployment of the
TG-Control gateway on a disposable host. This document describes what the image
and the Compose stack actually do, how to verify them, and which claims are
*not* evidenced. It does not authorize a production or internet-facing
deployment, and nothing here approves real Telegram accounts, credentials or
data.

Labels used throughout:

- **Executed locally** — a command in this repository was run and its result
  recorded (`docs/STATUS.md`, `docs/TRACEABILITY.md`).
- **Executed in a container** — the same command was run against a built image
  under a real container runtime.
- **Blocked** — cannot be executed in this environment; the reason is recorded
  instead of a claim.

## 1. What runs

| Component | Image | Role | Ports |
|---|---|---|---|
| `gateway` | `tg-control:local` (built from `Dockerfile`) | API, MCP-over-HTTP, owner panel, scheduler, instance guard | `127.0.0.1:8000` only |
| `db` | `postgres:17.9-bookworm` | metadata, audit, jobs, workflows, events | none published |
| `migrate` | `tg-control:local`, profile `tools` | one-shot Alembic runner, never automatic | none |

The gateway is a single process (`--workers 1`). It is **not** a horizontally
scalable deployment: a cooperative database advisory lock (`instance_guard`)
makes a second writer report a lost guard, and Compose starts exactly one
gateway. Do not add replicas and do not publish the port beyond loopback.

## 2. Build

```bash
docker build -t tg-control:local .
```

Properties enforced by the Dockerfile and asserted by `tests/test_deployment.py`:

- Two stages. Only the builder installs dependencies, from
  `requirements.lock` with `pip install --require-hashes`; the runtime stage has
  no compiler toolchain, no pip cache, no lock file and no build context.
- `python:3.11-slim-bookworm`, pinned to a patch-level tag. **Pinning by digest is
  an open release-checklist item** (see `docs/RELEASE_PROCESS.md`); it requires
  registry access that the current environment does not have.
- Unprivileged fixed identity `10001:10001` (`useradd --system`, shell
  `/usr/sbin/nologin`, no home directory) and `USER 10001:10001`.
- `/var/lib/tg-control/{synthetic,media,tmp}` created `0700` and owned by that
  UID.
- No secret, token, session, dump or `.env` file copied into any layer, and no
  `ENV` carrying credentials. Secrets are mounted files at runtime.
- `STOPSIGNAL SIGTERM`, uvicorn PID 1, `--timeout-graceful-shutdown 30`.
- `HEALTHCHECK` runs `python -m tg_control.healthcheck`, which calls the
  **readiness** endpoint with the owner credential. A process that merely accepts
  connections is not healthy.

Build evidence: **Blocked**. No Docker/Podman/Buildah binary, no container
socket and no effective capabilities (`CapEff: 0`) exist in the development
sandbox. The Dockerfile is reviewed statically and is exercised by the hosted CI
workflow, but it has not been built locally.

## 3. Configure

Secrets are files, never environment literals and never Compose values:

```bash
python scripts/init_synthetic_secrets.py --directory ./secrets
chmod 700 secrets
# Compose mounts these files into the container as bind mounts, which preserve
# the host owner and mode. The image runs as UID 10001, so that user must be able
# to read them: 0600 root-owned files fail with "secret file unreadable".
sudo chown -R 10001:10001 secrets
chmod 400 secrets/*
```

That writes six synthetic files (database URL, owner token, encryption key,
backup database URL, backup encryption key, backblaze-style placeholders where
used). It refuses to overwrite existing files, prints nothing and sets `0600`;
the ownership/mode above is what makes them readable by the container user while
staying unreadable to everything else.

| Variable | Required | Purpose |
|---|---|---|
| `TG_DATABASE_URL_FILE` (or `TG_DATABASE_URL`) | yes | PostgreSQL DSN as a file |
| `TG_OWNER_TOKEN_FILE` | yes | owner bearer credential |
| `TG_ENCRYPTION_KEY_FILE` | yes | Fernet key for synthetic state/media |
| `TG_SYNTHETIC_STATE_DIR` | yes in Compose | encrypted synthetic account state |
| `TG_MEDIA_STAGING_DIR` | optional | media staging (tmpfs here) |
| `TG_ALLOWED_HOSTS` | optional | Host allowlist, default `localhost,127.0.0.1` |
| `TG_ALLOWED_ORIGINS` | optional | browser Origin allowlist |
| `TG_LOG_LEVEL` | optional | `INFO` by default; `DEBUG` is never enabled silently |
| `TG_HEALTH_PORT` | optional | health-probe port inside the container |

Missing required configuration fails startup with
`missing required configuration: TG_DATABASE_URL_FILE (or TG_DATABASE_URL), …`.
The message names variables only — never a value or a path. `TG_..=value`
literals are not used by the Compose stack, so `docker compose config` never
prints a credential.

`.env.example` contains placeholders and non-secret tunables only; copy it to
`.env` if you want to override hosts/origins/UID. `.env`, `secrets/`, `state/` and
`*.tgcbak` are git- and Docker-ignored.

## 4. Persistent state and ownership

| Host path | Container path | Contents | Required owner |
|---|---|---|---|
| `./state/postgres` | `/var/lib/postgresql/data` | database cluster | `${TG_UID:-10001}` |
| `./state/synthetic` | `/var/lib/tg-control/synthetic` | encrypted synthetic account state | `10001` |
| `./var/lib/tg-control/media` | tmpfs | ephemeral media staging | `10001` |

Both containers run as `10001` because the `db` image entrypoint drops to the
configured UID; a root-owned bind mount breaks both. Prepare it explicitly:

```bash
mkdir -p state/postgres state/synthetic
sudo chown -R 10001:10001 state
chmod 700 state state/postgres state/synthetic
```

`state/synthetic` must not be group- or world-accessible: the application
refuses to open a synthetic state directory with looser permissions (asserted by
`tests/test_runtime_lifecycle.py` scenario `insecure_state_dir_refused`).

## 5. Start, verify, stop

```bash
# 0. read-only preflight: configuration, file privacy, disk, database, schema
docker compose run --rm --no-deps --entrypoint python gateway -m tg_control.preflight

# 1. migrations are an explicit one-shot step
docker compose run --rm migrate

# 2. start the stack and wait for health
docker compose up -d --wait gateway

# 3. verify
TOKEN="$(cat secrets/owner_token)"
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/health/live
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/health/ready
curl -fsS -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/health/dependencies

# 4. inspect safely
docker compose ps
docker compose logs --tail 100 gateway     # no secrets appear

# 5. graceful stop
docker compose stop gateway                # SIGTERM, 45 s grace period
docker compose down                        # keeps ./state
```

Health semantics:

| Endpoint | Requires owner token | Meaning |
|---|---|---|
| `/health/live` | yes | process and event loop are serving |
| `/health/ready` | yes | database reachable, schema revision matches this build, expected tables present, instance guard held |
| `/health/dependencies` | yes | adapter/dependency view used by orchestrators that distinguish degraded from dead |

Readiness returns `503` with `checks.migration != "current"` or
`checks.schema_objects != "complete"`. Audit writes are **fail-closed**, so an
unreachable database makes all three routes return `503`
(`audit_unavailable`, or `instance_guard_lost` once the advisory lock drops) —
the container health check therefore turns unhealthy during a database outage
instead of reporting a healthy process that cannot record anything. That is the
intended behaviour and is asserted by the
`database_outage_fail_closed` lifecycle scenario.

Stop behaviour: uvicorn installs a SIGTERM handler, drains in-flight requests,
runs the lifespan shutdown (scheduler stop, workflow recovery, engine dispose)
and exits. uvicorn re-raises the captured signal after a completed graceful
shutdown, so the process status is `-SIGTERM`; the decisive evidence is the
`Application shutdown complete` log line together with `forced_kill = false` in
the lifecycle harness. Compose allows 45 s, longer than the 30 s in-process
window, and Docker sends SIGKILL only after that.

## 5a. Deployment preflight

`tg-control-preflight` (module `tg_control.preflight`) is a read-only check an
operator can run on the host or inside the image before starting the gateway. It
never binds a port, migrates, writes application state or prints a secret value.

| Check | Failure code | Detail vocabulary |
|---|---|---|
| required variables present and settings valid | exit 1 | `missing required configuration: <vars>`, validation message |
| secret files are private regular files | exit 1 | `missing`, `unreadable`, `not_a_regular_file`, `group_or_world_accessible`, `empty` |
| optional backup key parses and differs from the encryption key | exit 1 config / warn | `invalid Fernet key`, `backup key must differ from the encryption key` |
| state directories exist, are private, writable, with headroom | exit 2 | `not_a_directory`, `not_writable_by_current_user`, `less than N MiB free` (persistent state: `--min-free-mb`, default 1024; ephemeral media staging: 64) |
| database reachable | exit 3 | `connection failed` |
| schema current with all tables | exit 3 | `missing`, `behind`, `ahead`, `unknown`, `incomplete` |

```bash
python -m tg_control.preflight            # human-readable lines
python -m tg_control.preflight --json     # machine-readable report
python -m tg_control.preflight --min-free-mb 4096
```

Exit codes are the startup contract (1 config, 2 filesystem, 3 database/schema,
4 unexpected). Executed evidence: `tests/test_preflight.py` (7 tests) covers the
passing case, missing configuration, group-readable secret files, insecure state
directory, unreachable database, stale schema, disk headroom and duplicate backup
key warning.

## 6. Migration policy (`require_current`)

- The application **never** migrates itself. There is no `alembic upgrade` on
  startup, no auto-repair and no destructive fallback.
- Startup inspects `alembic_version` and the expected table set before serving.
  States: `current`, `behind`, `ahead`, `missing`, `unavailable`, plus
  `objects: complete|incomplete`. Anything other than `current` + `complete`
  refuses startup with an actionable message and a `startup.schema_rejected`
  critical log line.
- A revision that cannot be ordered (unknown/external fork) is reported as
  `unknown` and refused; it is never assumed compatible.
- Migrations run explicitly: `docker compose run --rm migrate` (profile `tools`)
  or `alembic upgrade head` from the CLI. `migrate` is read-only apart from the
  database, drops all capabilities and is never part of `up`.
- Upgrade path verified: `0003 → head (0007)` with existing audit rows preserved
  (`tests/test_migrations.py`). Rollback path verified on a disposable database:
  `downgrade -1` removes exactly the newest revision and reapplies cleanly
  (`test_migration_rollback_and_reapply_on_disposable_sqlite`). Downgrades that
  would drop columns are **not** exercised and must not be claimed safe.
- Concurrent startup while a migration is running is handled by the same gate
  (the second process sees `behind`/`incomplete` and refuses), but it has not
  been load-tested and the `migrate` service must not run while a gateway serves
  traffic.
- A partially applied schema (revision matches, table missing) is detected and
  refused; `alembic upgrade head` is a no-op in that state, so recovery is a
  restore from a verified backup or a deliberate rebuild of a disposable
  database. Interrupted migrations are therefore fail-closed, not silently
  half-served.

App rollback and database rollback are different operations: rolling the image
back to a build that expects an older revision requires the database to be at
that revision too. See `docs/RELEASE_PROCESS.md` §5 for the decision table.

## 7. Failure modes and expected behaviour

Executed each time `tests/test_runtime_lifecycle.py` (host process) or
`scripts/container_smoke.sh` (container, needs a daemon) runs:

| Scenario | Expected | Evidence |
|---|---|---|
| Missing required config | exit 1, `missing required configuration: …`, names variables only | host process, executed |
| Invalid config (short token, bad Fernet key, bad DSN) | exit 1, actionable message, no value printed | host process, executed |
| Database unreachable | exit 3, message names connectivity | host process, executed |
| Schema behind/ahead/unknown/incomplete | exit 3, `startup.schema_rejected`, no migration attempted | host process, executed |
| Synthetic state dir group/world-accessible | exit 1, refuses to serve | host process, executed |
| Database stopped while serving | all health routes `503` (fail-closed audit), recovery after restart | host process, executed (`database_outage_fail_closed`); container variant in the smoke script (not executed here) |
| SIGTERM while serving | graceful shutdown, `forced_kill: false` | host process, executed |
| Restart / recreate | data persisted in `./state`, readiness returns | host process restart executed; container recreation blocked |

Secret-leak checks: the host-process harness scans the whole captured log for
the owner token, database URL password and encryption key (`log_secret_scan`,
executed, no leaks). `scripts/container_smoke.sh` performs the same scan over
`docker compose logs`.

## 8. Verification status

| Check | Status |
|---|---|
| Dockerfile/Compose static assertions | **Executed locally** (`tests/test_deployment.py`, 10 tests) |
| Deployment preflight (config, permissions, disk, database, schema) | **Executed locally** (`tests/test_preflight.py`, 7 tests) |
| Compose YAML parse + security posture | **Executed locally** (`tests/test_operational_config.py`) |
| Host-process lifecycle, health, graceful stop, restart persistence, database-outage fail-closed, failure modes | **Executed locally** (`tests/test_runtime_lifecycle.py` → `scripts/runtime_smoke.py`, 11 scenarios) |
| Image build, `docker compose config`, container start/recreate, in-container non-root check, log scan | **Blocked** — no container runtime in the development sandbox (`CapEff: 0`, no socket) |
| Hosted CI execution of the workflow | **Not executed** — `.github/workflows/ci.yaml` exists and is statically checked, but no run has happened on GitHub |
| Dependency vulnerability audit of the lockfile | **Executed locally** (`pip-audit -r requirements.lock --strict` → no known vulnerabilities); not yet run by hosted CI |
| Image/OS vulnerability scan, digest pinning | **Blocked / requires owner action** (network + registry approval) |
| Graceful shutdown under sustained load, concurrent startup/migration, long-running soak | **Not tested** — the shutdown path is verified with idle/short requests only |
| Any deployment to a real host | **Not performed and not authorized** |

Do not describe this stack as production-ready, hardened against a hostile
network, or verified on a container runtime until the blocked rows above have
real evidence.

## 9. Resource and DoS boundaries

- `gateway`: `mem_limit 512m`, `cpus 1.0`, `pids_limit 256`, tmpfs `/tmp` 64 MiB
  and media staging 256 MiB (`noexec,nosuid`), read-only root filesystem with
  only the two tmpfs mounts writable.
- `db`: `mem_limit 1g`, `pids_limit 512`, `shm_size 256m`.
- Both: `json-file` logging capped at 10 MiB × 3 files, so a log flood cannot
  fill the disk; `cap_drop: ALL`, `no-new-privileges`, no Docker socket, no host
  network, no published database port.
- Application-level bounds (request size, pagination, job/workflow limits,
  retention pruning) are unchanged by this package; see `docs/OPERATIONS.md`.

## 10. Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| `missing required configuration: …` | secret file missing or env var unset | create files with `scripts/init_synthetic_secrets.py`, check mounts |
| `schema is not initialized` | empty/new database | `docker compose run --rm migrate` |
| `schema revision … does not match this build` | image and database differ | run the documented migration, or deploy the matching image |
| `schema is incomplete` | interrupted restore or manual change | restore a verified backup, do not hand-edit tables |
| readiness `503`, liveness `200` | database down or schema drift | `docker compose ps`, `docker compose logs db` |
| container restarts repeatedly | startup rejection (see logs) | fix configuration; the gateway is designed to fail closed |
| permission denied on `state/` | bind mount not owned by 10001 | `sudo chown -R 10001:10001 state` |
| preflight reports `secret file unreadable` (or the gateway exits 1 on start) | secret files are 0600 and owned by the invoking user, but Compose bind-mounts them into a UID-10001 container | `sudo chown -R 10001:10001 secrets && chmod 700 secrets && chmod 400 secrets/*` |
| `instance_guard lost` in diagnostics | a second instance is running | stop the duplicate; single-instance only |
