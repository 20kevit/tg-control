# Backup, restore and disaster recovery

The existing manifest-v2 backup mechanism (`src/tg_control/backup.py`,
`tg_control.backup` CLI) is the only supported one. This document describes what
it includes, what it guarantees, how to prove a restore, and what it does **not**
prove.

> Verification success is not restoration success. `verify` authenticates and
> validates an archive without touching any target; a restore drill is the only
> evidence that data can actually be recovered. Both were executed on disposable
> databases (`tests/test_backup.py`, `tests/test_disaster_recovery.py`).

## 1. Included and excluded

| Included | Excluded |
|---|---|
| Complete PostgreSQL logical dump (`pg_dump --format=custom --no-owner --no-privileges`): accounts, clients, credentials (hashes), grants, settings, audit events, events/subscriptions/dead letters, jobs and attempts, workflow definitions/runs, execution keys, media metadata | Owner and client bearer tokens in plaintext (only hashes are stored), Telegram session material (never present in the synthetic system), OS files, `.env` files, secret files |
| Per-account encrypted synthetic state (`state/<uuid>/state.enc`) when `--state-dir` is supplied | Media staging bytes (ephemeral by design), logs, metrics, caches, container images |
| `manifest.json` v2: format version, `includes_state`, `app_version`, `schema_revision`, `created_at` | Restore targets, backup keys, database credentials |

Manifest v1 archives (no revision/app version) are still readable and raise a
warning; they cannot be checked for schema compatibility.

## 2. Cryptography and file handling

- Container format: `TGCBACK1` magic + 12-byte random nonce + AES-GCM
  ciphertext + 16-byte authentication tag. The header is authenticated as
  additional data.
- The archive is fully authenticated **before** it is parsed. A single flipped
  byte is detected as `invalid backup format` or `truncated backup`; there is no
  partial plaintext left behind.
- The 32-byte backup key is read from a `0600` file (`--backup-key-file`) and is
  never printed, logged or embedded in the archive.
- Output archives are created `O_CREAT|O_EXCL|O_NOFOLLOW`, mode `0600`. Plaintext
  scratch (dump, decrypted tar) is written `0600` into a temporary directory,
  preferably on a tmpfs (`--scratch-dir`).
- An interrupted or failed backup leaves the **incomplete encrypted file** in
  place deliberately: unlinking by pathname could delete a substituted file.
  Such a file fails verification.
- The synthetic-state lock (`.lock`, `0600`) prevents a concurrent backup while
  the same state directory is in use.

## 3. Create

The gateway must be **stopped** (or at least idle): there is no online snapshot
coordination and no cross-database transaction guarantee.

```bash
python -m tg_control.backup create \
  --database-url-file secrets/database_url \
  --backup-key-file secrets/backup_key \
  --state-dir state/synthetic \
  --scratch-dir /dev/shm \
  --output backups/gateway-$(date -u +%Y%m%dT%H%M%SZ).tgcbak
```

`--state-dir` is optional; omit it for a database-only archive
(`includes_state: false`). Use `--pg-bin-dir` when `pg_dump` is not on `PATH`.

## 4. Verify (read-only)

```bash
python -m tg_control.backup verify \
  --backup-key-file secrets/backup_key \
  --input backups/gateway-….tgcbak \
  --expected-schema-revision 0007 \
  --json
```

Checks performed:

1. Magic/version, nonce and GCM tag (authentication of the whole archive).
2. Tar layout: only regular files, no duplicate names, exactly
   `manifest.json` + `database.dump` (+ `state/<uuid>/state.enc` members).
3. Manifest schema: known manifest version, bounded field sizes, exact key set.
4. Dump is non-empty; state member presence matches `includes_state`.
5. When `--expected-schema-revision` is supplied and differs: `compatible: false`
   with a warning (verification still reports `integrity: verified` — the bytes
   are intact, the archive is simply not for this build).

`verify` never writes to a restore target and never prints key material. Passing
it proves *integrity and layout*, nothing about recoverability of the data.

## 5. Restore (separate target only)

Restore refuses to overwrite an active database. It requires an empty target and
the explicit `--confirm-empty-target` flag, and a **new** state directory:

```bash
# 1. create a new empty database and a new state directory on a disposable host
createdb -h 127.0.0.1 -p 55432 -U tgcontrol recovered
install -d -m 700 state/recovered

# 2. restore into the new targets
python -m tg_control.backup restore \
  --database-url-file secrets/restore_database_url \
  --backup-key-file secrets/backup_key \
  --state-dir state/recovered \
  --scratch-dir /dev/shm \
  --input backups/gateway-….tgcbak \
  --confirm-empty-target

# 3. verify schema state and start the gateway against the restored pair
alembic upgrade head          # only if the archive is older than this build
python -m uvicorn tg_control.api:app_factory --factory --host 127.0.0.1 --port 8001
```

Safety properties:

- The target database must contain no user tables; a non-empty target is refused
  before anything is written.
- A missing/duplicate `--state-dir` for a state-bearing archive is refused; the
  state directory is created fresh, never merged into an existing one.
- The archive is authenticated before restore begins, so a corrupted or
  truncated archive cannot leave a half-restored database behind without an
  error.

## 6. Restore drill (executed evidence)

`tests/test_disaster_recovery.py` seeds a source database and state directory with
synthetic data (account, client, policy, executed send, durable job, workflow and
run, event subscription, settings), then:

1. creates a backup, then `verify_archive` → `integrity: verified`,
   `compatible: True`;
2. restores into a **separate** database and a **separate** state directory (the
   source is untouched);
3. compares 15 tables row-by-row counts, digests and column values between source
   and restored database;
4. starts the API on the restored pair and serves `/v1/accounts`,
   `/v1/admin/jobs/{id}`, workflow runs and audit events;
5. re-runs the restore against the now non-empty target → refused;
6. tampers with one byte of the archive → `BackupError`, and no target directory
   is created;
7. rewrites the manifest schema revision → `compatible: False`, restore refused;
8. simulates an interrupted backup (truncated file) → verification fails;
9. checks an older-schema archive is refused until `alembic upgrade head` makes
   the database current, after which the application starts.

`tests/test_backup.py` additionally exercises the CLI as a subprocess, includes
state-bearing archives, and covers the fail-closed cases above.

## 7. Upgrade and downgrade interaction

| Situation | Action |
|---|---|
| Archive revision == running build | restore and start |
| Archive revision older than build | restore, then `alembic upgrade head` (verified `0003 → 0007` with existing audit rows) |
| Archive revision newer than build | **do not restore**; deploy the matching image first, or accept data loss explicitly |
| Unknown/foreign revision | refuse; inspect the archive origin |
| Downgrade of a live database | only where a tested downgrade exists (`0007 → 0006` verified on a disposable database); a restore is usually safer |

## 8. Guarantees and non-guarantees

Guaranteed and tested:

- Confidentiality at rest with the configured key; authenticated encryption
  detects any modification to the archive.
- Restoring into a clean target reproduces counts, digests and served API
  responses for the covered tables.
- Corrupt, truncated, incompatible or wrong-key archives are rejected before any
  target is modified.
- Interrupted backups are not silently accepted as valid.

Not guaranteed / not tested:

- Atomicity across database and state directory: a failure between the two
  writes leaves a database restored without its state (or the reverse). Treat the
  pair as one unit and re-run the restore from scratch on failure.
- A point-in-time snapshot: writes during backup are not coordinated. Stop the
  gateway first.
- Encryption-key rotation, offline key escrow and backup retention automation.
- Restore into a live database (intentionally unsupported) and migration of an
  archive produced by a future, incompatible manifest version.
- Proveability that the dump contains no unexpected object kinds (EXCLUDE /
  policy check remains the boundary).

## 9. Retention and operational hygiene

- Keep at least two independent, encrypted copies off-host; the repository itself
  must never contain an archive (`.gitignore`, `.dockerignore`).
- Files are `0600`; the enclosing directory should be `0700`. The synthetic state
  directory must not be group/world accessible.
- Test a restore on a disposable pair before relying on an archive. An untested
  archive is a hypothesis, not a backup.
- Rotating keys changes the archive set; keep the key that matches each archive
  and never store a key next to the archive.
