# Operations: synthetic-only development

**Do not deploy or connect real Telegram accounts/data.** Production, remote access, credentials, external infrastructure, destructive actions and firewall/DNS changes require separate explicit owner approval and the unresolved compliance decision. The following is a local development procedure, not a production-ready deployment. Docker/Compose have not been run in the current environment; verify them on a disposable local machine before use. See [status](STATUS.md).

## Local Docker setup

Requirements: Docker Compose v2, non-root host user, Python for local secret generation, and a private working directory. The pinned image tags can move; release builds must pin reviewed image digests and rescan OS packages. Start exactly **one gateway process/worker/instance**. No Telegram library is enabled.

1. From the repository, create private, ignored directories (`state/`, `secrets/`, `backups/`) without overwriting any existing data. Ensure `state/postgres`, `state/synthetic` and `secrets` are owned by the current non-root user and mode 0700. Never share or commit the generated files.
2. Provision **new synthetic-development-only** files `secrets/postgres_password`, `secrets/database_url`, `secrets/backup_database_url`, `secrets/owner_token`, `secrets/encryption_key` and `secrets/backup_key`, each mode 0600 and owned by the current user. To generate them locally without printing or overwriting values, install the Python development environment and run `.venv/bin/python scripts/init_synthetic_secrets.py --directory ./secrets`. The script refuses existing target files. The PostgreSQL password and owner token are independently generated high-entropy random values; encryption and backup keys are independent Fernet keys. The URL uses the same generated password. Never paste values into chat, shell history, Git, or AI clients. Compose file-backed secrets are bind mounts; matching host UID/GID is required.
3. Export only non-secret numeric IDs, e.g. `export TG_UID="$(id -u)" TG_GID="$(id -g)"`. Do not use UID 0. Then, after reviewing the compose file:

```sh
docker compose up -d --wait db
docker compose run --rm --no-deps gateway python -m alembic upgrade head
docker compose up -d gateway
```

Open `http://127.0.0.1:8000/admin/` locally. Uvicorn listens inside the container but Compose publishes only loopback; PostgreSQL has no published port by default. Migrations are an explicit operation, never automatic at startup. Startup fails closed if the schema revision is not current. To inspect health use `/health/live` (process) and `/health/ready` (database connectivity). Never configure access/body/authorization logging or trust proxy headers. Check container health, storage capacity, audit failures and `media maintenance failed` without recording secrets/content.

The panel polls synthetic session status only while open. It is not push/email monitoring. For any approved remote design, TLS termination, a restricted ingress, exact allowed Host/Origin values and a separate security review are mandatory; static MCP Bearer credentials do not implement OAuth.

## Encrypted backup

Backups are **not automated**. Schedule them only with owner approval, keep encrypted copies off-host and separately protect both keys. The backup key is a second independent 32-byte URL-safe Base64/Fernet-format key in a 0600 file. The original gateway `encryption_key` is **not included** in the archive and must also be backed up securely, separately. Losing either key makes recovery impossible. The archive contains a PostgreSQL custom dump, policies, audit and optional encrypted per-account synthetic state. It does not contain the owner credential file; preserve that separately as needed.

Stop the gateway and any other writer first; the tool rejects a held synthetic state lock but cannot detect every possible database writer. Use compatible `pg_dump`/`pg_restore` binaries at least as new as the PostgreSQL server. The tool uses a 0600 temporary libpq service file rather than passing credentials in command arguments. It writes **plaintext temporary dumps** during backup and plaintext archives/dumps during restore; select a sufficiently large private tmpfs as `--scratch-dir` (for example `/dev/shm` on Linux), protect swap/core dumps, and inspect cleanup after interruptions. Filesystem snapshots and backups can retain expired/deleted content; manage their lifecycle separately. Do not store archives in the repository.

For the provided Compose setup, the optional `compose.backup.yaml` binds the DB to `127.0.0.1:55432` **only** while that override is used. After stopping the gateway (`docker compose stop gateway`), run `docker compose -f compose.yaml -f compose.backup.yaml up -d --wait db` to make the local DB available, and use the generated private `secrets/backup_database_url`, which points to `127.0.0.1:55432` with the same DB credentials; keep it outside Git. Run the installed host tool, pointing `--pg-bin-dir` at compatible PostgreSQL client binaries if necessary:

```sh
.venv/bin/tg-control-backup create --database-url-file ./secrets/backup_database_url \
  --backup-key-file ./secrets/backup_key --state-dir ./state/synthetic \
  --scratch-dir /dev/shm --output ./backups/synthetic-001.tgcbak
```

Existing archives are never overwritten. If the simulator was ephemeral, omit `--state-dir` (only metadata is saved). Disable the backup port afterwards by recreating the DB service with `docker compose up -d --wait db`, then restart the gateway with `docker compose up -d gateway`; make no changes to external ingress. The DB service volume must remain intact; **never use** `docker compose down -v` as a routine operation.

## Restore and recovery

Restoring changes data and requires explicit owner approval. Test only against newly created disposable targets until approved. Create a **new, empty** PostgreSQL database and a private connection file for it; choose a **nonexistent** state directory. Stop the gateway and all writers. Confirm the intended target, then:

```sh
.venv/bin/tg-control-backup restore --database-url-file /private/new-empty-db-url \
  --backup-key-file /private/backup-key --input /private/backups/synthetic-001.tgcbak \
  --state-dir /private/new-synthetic-state --scratch-dir /dev/shm --confirm-empty-target
```

Omit `--state-dir` only for an archive without synthetic state. Authentication is verified before archive parsing, archive paths are restricted, an existing/non-empty target is rejected, and `pg_restore` uses a single transaction. Database and state restoration together are **not atomic**: a filesystem failure after database restore can leave partial targets. Do not overwrite or automatically delete them; inspect/retry only with owner approval. Restore the original gateway encryption key and point the gateway at the new DB/state; validate credentials, policies, audit and media using synthetic data. Backups may be older than current credentials or policies: revoke/rotate credentials and reconcile operations as appropriate. Test restore regularly into isolated targets.

## Migrations and troubleshooting

Back up and test migrations on an isolated copy before upgrading. Use the exact checked-out code version and `TG_DATABASE_URL_FILE=/private/url python -m alembic upgrade head` from the repository. Downgrades may lose security metadata and are not a substitute for backup; do not run them without explicit approval. If readiness fails, check DB reachability and migration revision, state-directory ownership/mode and exclusive lock, secret file access, and disk capacity. If media maintenance fails, stale bytes may remain until the next successful sweep; resolve the error and verify retention. A mutation with a lost response or audit failure may have occurred: reconcile rather than blindly retrying.
