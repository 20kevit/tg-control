"""Real encrypted backup/restore of a synthetic gateway on private PostgreSQL sockets."""

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

from tg_control.adapters.synthetic_store import SyntheticStore
from tg_control.api import create_app
from tg_control.backup import BackupError, create_backup, restore_backup
from tg_control.config import Settings

ROOT = Path(__file__).resolve().parents[1]
PG_BIN = Path(pgserver.__file__).parent / "pginstall" / "bin"


def secret(path, content):
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    path.chmod(0o600)
    return path


def test_encrypted_postgres_backup_restore_and_negative_paths(tmp_path):
    server = pgserver.get_server(tmp_path / "backup-pgdata", cleanup_mode="delete")
    try:
        with psycopg.connect(server.get_uri(), autocommit=True) as db:
            db.execute("CREATE DATABASE backup_source")
            db.execute("CREATE DATABASE backup_target")
            db.execute("CREATE DATABASE backup_cli_target")
        source_uri, target_uri = server.get_uri("backup_source"), server.get_uri("backup_target")
        source_url = source_uri.replace("postgresql://", "postgresql+psycopg://", 1)
        target_url = target_uri.replace("postgresql://", "postgresql+psycopg://", 1)
        source_file = secret(tmp_path / "source-uri", source_url)
        target_file = secret(tmp_path / "target-uri", target_url)
        cli_target_url = server.get_uri("backup_cli_target").replace(
            "postgresql://", "postgresql+psycopg://", 1
        )
        cli_target_file = secret(tmp_path / "cli-target-uri", cli_target_url)
        key_file = secret(tmp_path / "backup-key", Fernet.generate_key())
        wrong_key = secret(tmp_path / "wrong-key", Fernet.generate_key())
        env = {**os.environ, "TG_DATABASE_URL": source_url}
        env.pop("TG_DATABASE_URL_FILE", None)
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
        )
        state = tmp_path / "synthetic-source"
        encryption_key = Fernet.generate_key()
        settings = Settings(
            source_url,
            "synthetic-owner-token-" + "x" * 32,
            encryption_key,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            synthetic_state_dir=state,
        )
        with TestClient(create_app(settings)) as client:
            owner, account, actor = create_resources(client, settings)
            set_policy(client, owner, account, actor)
            response = execute(
                client,
                actor["credential"],
                account,
                "send_media",
                "public-channel",
                key=uuid.uuid4(),
                text="synthetic backup marker",
                media={
                    "filename": "test.txt",
                    "content_type": "text/plain",
                    "data_base64": "c3ludGhldGljIGJ5dGVz",
                },
            )
            message_id = response.json()["items"][0]["data"]["message_id"]
            assert (
                client.put(
                    "/v1/admin/settings",
                    headers=owner,
                    json={
                        "audit_retention_days": 90,
                        "cache_ttl_seconds": 0,
                        "media_retention_days": None,
                    },
                ).status_code
                == 200
            )

        output = tmp_path / "archive.tgcbak"
        held = SyntheticStore(state, encryption_key)
        try:
            with pytest.raises(BackupError, match="stop the gateway"):
                create_backup(source_file, key_file, output, state_dir=state, bin_dir=PG_BIN)
        finally:
            held.close()
        create_backup(source_file, key_file, output, state_dir=state, bin_dir=PG_BIN)
        encrypted = output.read_bytes()
        assert b"synthetic backup marker" not in encrypted
        assert actor["credential"].encode() not in encrypted
        assert output.stat().st_mode & 0o077 == 0
        with pytest.raises(FileExistsError):
            create_backup(source_file, key_file, output, state_dir=state, bin_dir=PG_BIN)

        cli_output, cli_state = tmp_path / "cli.tgcbak", tmp_path / "cli-state"
        base = [sys.executable, "-m", "tg_control.backup"]
        created = subprocess.run(
            base
            + [
                "create",
                "--database-url-file",
                str(source_file),
                "--backup-key-file",
                str(key_file),
                "--state-dir",
                str(state),
                "--output",
                str(cli_output),
                "--pg-bin-dir",
                str(PG_BIN),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        restored_cli = subprocess.run(
            base
            + [
                "restore",
                "--database-url-file",
                str(cli_target_file),
                "--backup-key-file",
                str(key_file),
                "--state-dir",
                str(cli_state),
                "--input",
                str(cli_output),
                "--pg-bin-dir",
                str(PG_BIN),
                "--confirm-empty-target",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        assert (
            actor["credential"]
            not in created.stdout + created.stderr + restored_cli.stdout + restored_cli.stderr
        )
        assert (cli_state / account["id"] / "state.enc").is_file()
        with psycopg.connect(server.get_uri("backup_cli_target")) as db:
            assert db.execute("SELECT count(*) FROM accounts").fetchone()[0] == 1

        restored = tmp_path / "synthetic-restored"
        with pytest.raises(BackupError, match="confirmation"):
            restore_backup(
                target_file,
                key_file,
                output,
                state_dir=restored,
                confirm_empty=False,
                bin_dir=PG_BIN,
            )
        with pytest.raises(BackupError, match="authentication"):
            restore_backup(
                target_file,
                wrong_key,
                output,
                state_dir=restored,
                confirm_empty=True,
                bin_dir=PG_BIN,
            )
        tampered = tmp_path / "tampered.tgcbak"
        corrupt = bytearray(encrypted)
        corrupt[len(corrupt) // 2] ^= 1
        tampered.write_bytes(corrupt)
        with pytest.raises(BackupError, match="authentication"):
            restore_backup(
                target_file,
                key_file,
                tampered,
                state_dir=restored,
                confirm_empty=True,
                bin_dir=PG_BIN,
            )
        assert not restored.exists()
        with pytest.raises(BackupError, match="not empty"):
            restore_backup(
                source_file,
                key_file,
                output,
                state_dir=restored,
                confirm_empty=True,
                bin_dir=PG_BIN,
            )
        assert not restored.exists()
        restore_backup(
            target_file, key_file, output, state_dir=restored, confirm_empty=True, bin_dir=PG_BIN
        )
        restored_settings = Settings(
            target_url,
            settings.owner_token,
            encryption_key,
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
            synthetic_state_dir=restored,
        )
        with TestClient(create_app(restored_settings)) as client:
            response = execute(
                client,
                actor["credential"],
                account,
                "get_media",
                "public-channel",
                message_id=message_id,
            )
            assert (
                response.json()["items"][0]["data"]["media"]["data_base64"]
                == "c3ludGhldGljIGJ5dGVz"
            )
            assert (
                client.get("/v1/accounts", headers=auth(actor["credential"])).json()["accounts"][0][
                    "id"
                ]
                == account["id"]
            )
            assert client.get("/v1/admin/audit", headers=auth(settings.owner_token)).json()[
                "events"
            ]
        with pytest.raises(BackupError, match="must not already exist"):
            restore_backup(
                target_file,
                key_file,
                output,
                state_dir=restored,
                confirm_empty=True,
                bin_dir=PG_BIN,
            )
    finally:
        server.cleanup()


def test_restore_rejects_authenticated_path_traversal_before_database_access(tmp_path):
    import base64
    import io
    import json
    import tarfile

    from tg_control.backup import _EncryptWriter, _tar_file

    key = Fernet.generate_key()
    key_file = secret(tmp_path / "key", key)
    url_file = secret(tmp_path / "uri", "postgresql://localhost/test")
    archive_path = tmp_path / "hostile.tgcbak"
    with archive_path.open("wb") as output:
        writer = _EncryptWriter(output, base64.urlsafe_b64decode(key))
        with tarfile.open(fileobj=writer, mode="w|") as archive:
            manifest = json.dumps({"version": 1, "includes_state": True}).encode()
            _tar_file(archive, "manifest.json", io.BytesIO(manifest), len(manifest))
            _tar_file(archive, "database.dump", io.BytesIO(b"x"), 1)
            _tar_file(archive, "state/../../escape", io.BytesIO(b"x"), 1)
        writer.finish()
    state = tmp_path / "restored"
    with pytest.raises(BackupError, match="invalid backup path"):
        restore_backup(
            url_file, key_file, archive_path, state_dir=state, confirm_empty=True, bin_dir=PG_BIN
        )
    assert not state.exists()
    assert not (tmp_path / "escape").exists()


def test_backup_secret_file_permissions_and_url_errors_are_sanitized(tmp_path):
    from tg_control.backup import _backup_key, _connection

    insecure = tmp_path / "key"
    insecure.write_bytes(Fernet.generate_key())
    insecure.chmod(0o644)
    with pytest.raises(BackupError, match="private"):
        _backup_key(insecure)
    invalid = secret(tmp_path / "uri", "invalid-connection-with-synthetic-password")
    with pytest.raises(BackupError) as error:
        _connection(invalid)
    assert "synthetic-password" not in str(error.value)
