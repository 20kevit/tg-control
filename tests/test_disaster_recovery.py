"""Disposable disaster-recovery drill: seed -> backup -> verify -> restore -> compare.

This is a *synthetic* exercise using a private temporary PostgreSQL server and
fixtures only. It proves the supported backup mechanism preserves the documented
metadata and optional encrypted simulator state, that a corrupted or
schema-incompatible archive is refused, and that the restored database can serve
a real API request. It does not prove production disaster recovery, atomic
database+state restoration, or anything about real Telegram data.
"""

import hashlib
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pgserver
import psycopg
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from test_api import auth, create_resources, execute, set_policy

from tg_control.api import create_app
from tg_control.backup import BackupError, create_backup, restore_backup, verify_archive
from tg_control.config import Settings
from tg_control.db import EXPECTED_SCHEMA_REVISION, make_engine, schema_state

ROOT = Path(__file__).resolve().parents[1]
PG_BIN = Path(pgserver.__file__).parent / "pginstall" / "bin"

TABLES = (
    "accounts",
    "clients",
    "credentials",
    "grants",
    "settings",
    "execution_keys",
    "audit_events",
    "events",
    "event_subscriptions",
    "event_attempts",
    "event_dead_letters",
    "jobs",
    "job_runs",
    "workflows",
    "workflow_runs",
)


def secret(path: Path, value) -> Path:
    path.write_bytes(value if isinstance(value, bytes) else str(value).encode())
    path.chmod(0o600)
    return path


def as_asyncpg_url(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


def migrate(url: str, revision: str = "head") -> None:
    env = {**os.environ, "TG_DATABASE_URL": url}
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )


def dump(url: str) -> dict[str, object]:
    """Snapshot the documented durable metadata for an invariant comparison."""
    engine = make_engine(url)
    state = schema_state(engine)
    snapshot: dict[str, object] = {"schema": state, "tables": {}}
    plain_url = url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(plain_url) as db:
        for table in TABLES:
            rows = db.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            columns = [
                description.name
                for description in db.execute(f"SELECT * FROM {table} LIMIT 0").description
            ]
            snapshot["tables"][table] = {
                "count": len(rows),
                "digest": hashlib.sha256(
                    json.dumps(
                        [[str(value) for value in row] for row in rows], default=str
                    ).encode()
                ).hexdigest(),
                "columns": columns,
            }
        snapshot["revision"] = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
    engine.dispose()
    return snapshot


def seed(url: str, owner_token: str, encryption_key: bytes, state_dir: Path) -> dict:
    settings = Settings(
        url,
        owner_token,
        encryption_key,
        allowed_hosts=("testserver",),
        allowed_origins=("http://testserver",),
        synthetic_state_dir=state_dir,
    )
    created: dict = {}
    with TestClient(create_app(settings)) as client:
        owner, account, actor = create_resources(client, settings)
        set_policy(client, owner, account, actor)
        credential = actor["credential"]
        message = execute(
            client,
            credential,
            account,
            "send",
            "public-channel",
            key=uuid.uuid4(),
            text="synthetic disaster-recovery marker",
        )
        assert message.status_code == 200, message.text
        created["message_id"] = message.json()["items"][0]["data"]["message_id"]
        created["account_id"] = account["id"]
        created["client_id"] = actor["id"]
        created["credential"] = credential

        job = client.post(
            "/v1/jobs",
            headers=auth(credential),
            json={
                "kind": "telegram.send",
                "input": {
                    "account_id": account["id"],
                    "chat_id": "public-channel",
                    "text": "synthetic scheduled marker",
                },
                "schedule": {"kind": "once", "run_at": "2030-01-01T00:00:00+00:00"},
                "deadline_seconds": 3600,
            },
        )
        assert job.status_code == 201, job.text
        created["job_id"] = job.json()["job_id"]

        workflow = client.post(
            "/v1/admin/workflows",
            headers=owner,
            json={
                "name": "synthetic recovery workflow",
                "client_id": actor["id"],
                "actions": [
                    {
                        "kind": "send",
                        "account_id": account["id"],
                        "chat_id": "public-channel",
                        "text": "synthetic workflow marker",
                    }
                ],
            },
        )
        assert workflow.status_code == 201, workflow.text
        created["workflow_id"] = workflow.json()["workflow_id"]
        run = client.post(
            f"/v1/admin/workflows/{created['workflow_id']}/run", headers=owner, json={}
        )
        assert run.status_code == 200, run.text
        created["run_id"] = run.json()["run_id"]

        subscription = client.post(
            "/v1/events/subscriptions",
            headers=auth(credential),
            json={
                "account_id": account["id"],
                "name": "synthetic recovery subscription",
                "event_types": ["message.new"],
                "chat_ids": ["public-channel"],
            },
        )
        assert subscription.status_code == 201, subscription.text
        created["subscription_id"] = subscription.json()["id"]

        settings_response = client.put(
            "/v1/admin/settings",
            headers=owner,
            json={
                "audit_retention_days": 30,
                "cache_ttl_seconds": 0,
                "media_retention_days": None,
            },
        )
        assert settings_response.status_code == 200, settings_response.text
    return created


def test_complete_synthetic_backup_verify_restore_and_compare(tmp_path, monkeypatch):
    server = pgserver.get_server(tmp_path / "dr-pgdata", cleanup_mode="delete")
    try:
        with psycopg.connect(server.get_uri(), autocommit=True) as db:
            db.execute("CREATE DATABASE dr_source")
            db.execute("CREATE DATABASE dr_target")
            db.execute("CREATE DATABASE dr_upgrade")

        source_url = as_asyncpg_url(server.get_uri("dr_source"))
        target_url = as_asyncpg_url(server.get_uri("dr_target"))
        source_file = secret(tmp_path / "source-url", source_url)
        target_file = secret(tmp_path / "target-url", target_url)
        key_file = secret(tmp_path / "backup-key", Fernet.generate_key())
        encryption_key = Fernet.generate_key()
        owner_token = "synthetic-owner-token-" + "d" * 32
        state_dir = tmp_path / "state-source"

        migrate(source_url)
        created = seed(source_url, owner_token, encryption_key, state_dir)
        before = dump(source_url)
        assert before["revision"] == EXPECTED_SCHEMA_REVISION
        assert before["schema"]["objects"] == "complete"
        for table in ("accounts", "clients", "jobs", "workflows", "workflow_runs", "audit_events"):
            assert before["tables"][table]["count"] > 0, table
        assert before["tables"]["event_subscriptions"]["count"] == 1

        archive = tmp_path / "dr-backup.tgcbak"
        create_backup(source_file, key_file, archive, state_dir=state_dir, bin_dir=PG_BIN)

        # 1. Verification is a read-only compatibility/integrity gate, not a restore.
        report = verify_archive(
            archive, key_file, state_dir=None, scratch_dir=tmp_path,
            expected_schema_revision=EXPECTED_SCHEMA_REVISION,
        )
        assert report["integrity"] == "verified"
        assert report["compatible"] is True
        assert report["schema_revision"] == EXPECTED_SCHEMA_REVISION
        assert report["manifest_version"] == 2
        assert report["includes_state"] is True
        assert report["state_accounts"] == 1
        assert report["dump_bytes"] > 0
        assert report["warnings"] and any("state-dir" in warning for warning in report["warnings"])
        encrypted = archive.read_bytes()
        for marker in (
            b"synthetic disaster-recovery marker",
            created["credential"].encode(),
            encryption_key,
        ):
            assert marker not in encrypted

        # 2. Restore into a separate disposable database and a new state directory.
        restored_state = tmp_path / "state-restored"
        restore_backup(
            target_file,
            key_file,
            archive,
            state_dir=restored_state,
            confirm_empty=True,
            bin_dir=PG_BIN,
        )
        assert (restored_state / created["account_id"] / "state.enc").is_file()

        # 3. Compare documented durable metadata and schema state.
        after = dump(target_url)
        assert after["revision"] == EXPECTED_SCHEMA_REVISION
        assert after["schema"] == {"state": "current", "revision": EXPECTED_SCHEMA_REVISION,
                                   "objects": "complete"}
        for table in TABLES:
            assert after["tables"][table]["count"] == before["tables"][table]["count"], table
            assert after["tables"][table]["digest"] == before["tables"][table]["digest"], table
            assert after["tables"][table]["columns"] == before["tables"][table]["columns"], table

        # 4. The restored instance must actually serve an authorized read.
        restored_settings = Settings(
            target_url,
            owner_token,
            encryption_key,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            synthetic_state_dir=restored_state,
        )
        with TestClient(create_app(restored_settings)) as client:
            accounts = client.get("/v1/accounts", headers=auth(created["credential"]))
            assert accounts.status_code == 200, accounts.text
            assert accounts.json()["accounts"][0]["id"] == created["account_id"]
            job = client.get(f"/v1/admin/jobs/{created['job_id']}", headers=auth(owner_token))
            assert job.status_code == 200 and job.json()["job_id"] == created["job_id"]
            history = client.get(
                f"/v1/admin/workflows/{created['workflow_id']}/runs", headers=auth(owner_token)
            )
            assert history.status_code == 200
            assert [run["run_id"] for run in history.json()["runs"]] == [created["run_id"]]
            audit = client.get("/v1/admin/audit?limit=5", headers=auth(owner_token))
            assert audit.status_code == 200 and audit.json()["events"]

        # 5. Re-running restore against the now-populated target is refused.
        with pytest.raises(BackupError, match="not empty|must not already exist"):
            restore_backup(
                target_file, key_file, archive, state_dir=restored_state,
                confirm_empty=True, bin_dir=PG_BIN,
            )

        # 6. Corruption is rejected before any target is touched.
        tampered = tmp_path / "tampered.tgcbak"
        corrupt = bytearray(encrypted)
        corrupt[len(corrupt) // 2] ^= 1
        tampered.write_bytes(corrupt)
        with pytest.raises(BackupError):
            verify_archive(tampered, key_file)
        untouched = tmp_path / "never-created"
        with pytest.raises(BackupError, match="authentication"):
            restore_backup(
                target_file, key_file, tampered, state_dir=untouched,
                confirm_empty=True, bin_dir=PG_BIN,
            )
        assert not untouched.exists()

        # 7. A schema-incompatible archive is detected by metadata, not by restore.
        other_db = "dr_upgrade"
        other_url = as_asyncpg_url(server.get_uri(other_db))
        other_file = secret(tmp_path / "upgrade-url", other_url)
        env = {**os.environ, "TG_DATABASE_URL": other_url}
        env.pop("TG_DATABASE_URL_FILE", None)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", EXPECTED_SCHEMA_REVISION],
            cwd=ROOT, env=env, check=True, capture_output=True,
        )
        old_archive = tmp_path / "old-revision.tgcbak"
        create_backup(other_file, key_file, old_archive, state_dir=None, bin_dir=PG_BIN)
        incompatible = verify_archive(
            old_archive, key_file, expected_schema_revision="9999_future"
        )
        assert incompatible["compatible"] is False
        assert any("differs" in warning for warning in incompatible["warnings"])

        # 8. An interrupted backup leaves an unauthenticated file that is refused.
        partial = tmp_path / "interrupted.tgcbak"
        with monkeypatch.context() as failing:
            def fail_finish(_writer):
                raise OSError("synthetic interrupt")

            failing.setattr("tg_control.backup._EncryptWriter.finish", fail_finish)
            with pytest.raises(OSError, match="synthetic interrupt"):
                create_backup(source_file, key_file, partial, state_dir=None, bin_dir=PG_BIN)
        assert partial.exists()
        with pytest.raises(BackupError):
            verify_archive(partial, key_file)

        # 9. The supported upgrade path turns the older database into a compatible one.
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT, env=env, check=True, capture_output=True,
        )
        upgraded = dump(other_url)
        assert upgraded["revision"] == EXPECTED_SCHEMA_REVISION
        assert upgraded["schema"]["objects"] == "complete"
    finally:
        server.cleanup()
