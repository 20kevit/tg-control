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


def test_encrypted_postgres_backup_restore_and_negative_paths(tmp_path, monkeypatch):
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
        partial = tmp_path / "partial.tgcbak"
        with monkeypatch.context() as failing:

            def fail_finish(_writer):
                raise OSError("synthetic interrupted write")

            failing.setattr("tg_control.backup._EncryptWriter.finish", fail_finish)
            with pytest.raises(OSError, match="interrupted"):
                create_backup(source_file, key_file, partial, state_dir=state, bin_dir=PG_BIN)
        assert partial.exists() and partial.stat().st_mode & 0o077 == 0
        assert b"synthetic backup marker" not in partial.read_bytes()
        with pytest.raises(BackupError, match="authentication"):
            restore_backup(
                target_file,
                key_file,
                partial,
                state_dir=tmp_path / "partial-state",
                confirm_empty=True,
                bin_dir=PG_BIN,
            )

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
        # A caller's ambient libpq service must not redirect the empty-target
        # check, and unrelated secrets must not be inherited by child tools.
        with monkeypatch.context() as poisoned:
            poisoned.setenv("PGSERVICE", "attacker-service")
            poisoned.setenv("PGSERVICEFILE", str(tmp_path / "does-not-exist"))
            poisoned.setenv("PGOPTIONS", "-c default_transaction_read_only=on")
            poisoned.setenv("TG_SYNTHETIC_SECRET_SENTINEL", "should-not-be-inherited")
            restore_backup(
                target_file,
                key_file,
                output,
                state_dir=restored,
                confirm_empty=True,
                bin_dir=PG_BIN,
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


def test_backup_tool_environment_excludes_ambient_secrets_and_libpq_overrides(
    tmp_path, monkeypatch
):
    from tg_control.backup import _service

    monkeypatch.setenv("TG_SYNTHETIC_SECRET_SENTINEL", "should-not-be-inherited")
    monkeypatch.setenv("PGHOST", "malicious-host")
    monkeypatch.setenv("PGOPTIONS", "-c statement_timeout=1")
    monkeypatch.setenv("PYTHONPATH", "/synthetic-injection")
    monkeypatch.setenv("LD_PRELOAD", "/synthetic-injection")
    with _service({"dbname": "synthetic"}, tmp_path) as environment:
        assert set(environment) == {"PATH", "HOME", "TMPDIR", "LANG", "PGSERVICE", "PGSERVICEFILE"}
        assert environment["HOME"] == str(tmp_path)
        assert environment["PGSERVICE"] == "tgcontrol"
        assert "synthetic" not in environment["PGSERVICEFILE"]
        assert (tmp_path / "pg_service.conf").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("version", [True, 1.0, "1"])
def test_authenticated_manifest_version_must_be_an_integer(tmp_path, version):
    import base64
    import io
    import json
    import tarfile

    from tg_control.backup import _EncryptWriter, _tar_file

    key = Fernet.generate_key()
    key_file = secret(tmp_path / "key", key)
    url_file = secret(tmp_path / "uri", "postgresql://localhost/test")
    archive_path = tmp_path / "invalid-version.tgcbak"
    with archive_path.open("wb") as output:
        writer = _EncryptWriter(output, base64.urlsafe_b64decode(key))
        with tarfile.open(fileobj=writer, mode="w|") as archive:
            manifest = json.dumps({"version": version, "includes_state": False}).encode()
            _tar_file(archive, "manifest.json", io.BytesIO(manifest), len(manifest))
            _tar_file(archive, "database.dump", io.BytesIO(b"x"), 1)
        writer.finish()
    with pytest.raises(BackupError, match="unsupported backup manifest"):
        restore_backup(
            url_file, key_file, archive_path, state_dir=None, confirm_empty=True, bin_dir=PG_BIN
        )


def _synthetic_archive(path, key, manifest, *, dump=b"PGDMP synthetic dump"):
    import base64
    import io
    import json
    import tarfile

    from tg_control.backup import _EncryptWriter, _tar_file

    with path.open("wb") as output:
        writer = _EncryptWriter(output, base64.urlsafe_b64decode(key))
        with tarfile.open(fileobj=writer, mode="w|") as archive:
            raw = json.dumps(manifest).encode()
            _tar_file(archive, "manifest.json", io.BytesIO(raw), len(raw))
            _tar_file(archive, "database.dump", io.BytesIO(dump), len(dump))
        writer.finish()
    return path


def _manifest(**overrides):
    manifest = {
        "version": 2,
        "includes_state": False,
        "app_version": "0.1.0",
        "schema_revision": "0008_retention",
        "created_at": "2026-01-01T00:00:00+00:00",
    }
    manifest.update(overrides)
    return manifest


def test_verify_reports_metadata_and_writes_nothing(tmp_path):
    from tg_control.backup import verify_archive

    key = Fernet.generate_key()
    key_file = secret(tmp_path / "key", key)
    archive = _synthetic_archive(tmp_path / "good.tgcbak", key, _manifest())

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    state_target = tmp_path / "restore-target"
    report = verify_archive(
        archive,
        key_file,
        state_dir=state_target,
        scratch_dir=scratch,
        expected_schema_revision="0008_retention",
    )
    assert report["format"] == "tg-control-encrypted-backup"
    assert report["manifest_version"] == 2
    assert report["app_version"] == "0.1.0"
    assert report["schema_revision"] == "0008_retention"
    assert report["includes_state"] is False
    assert report["state_accounts"] == 0
    assert report["dump_bytes"] == len(b"PGDMP synthetic dump")
    assert report["archive_bytes"] == archive.stat().st_size
    assert report["integrity"] == "verified"
    assert report["compatible"] is True
    assert report["warnings"] == []
    assert not state_target.exists()
    assert list(scratch.iterdir()) == []


def test_verify_flags_legacy_and_revision_mismatch(tmp_path):
    from tg_control.backup import verify_archive

    key = Fernet.generate_key()
    key_file = secret(tmp_path / "key", key)

    legacy = _synthetic_archive(
        tmp_path / "legacy.tgcbak", key, {"version": 1, "includes_state": False}
    )
    report = verify_archive(legacy, key_file)
    assert report["manifest_version"] == 1
    assert report["compatible"] is True
    assert report["schema_revision"] is None
    assert any("manifest v1" in warning for warning in report["warnings"])
    assert not any("newer" in warning for warning in report["warnings"])

    modern = _synthetic_archive(tmp_path / "modern.tgcbak", key, _manifest())
    mismatched = verify_archive(modern, key_file, expected_schema_revision="0009_other")
    assert mismatched["compatible"] is False
    assert any("differs" in warning for warning in mismatched["warnings"])
    unknown = verify_archive(legacy, key_file, expected_schema_revision="0009_other")
    assert unknown["compatible"] is True
    assert any("unknown" in warning for warning in unknown["warnings"])


def test_verify_state_archive_requires_a_new_state_dir(tmp_path):
    import base64
    import io
    import json
    import tarfile
    import uuid

    from tg_control.backup import _EncryptWriter, _tar_file, verify_archive

    key = Fernet.generate_key()
    key_file = secret(tmp_path / "key", key)
    account_id = str(uuid.uuid4())
    archive = tmp_path / "state.tgcbak"
    with archive.open("wb") as output:
        writer = _EncryptWriter(output, base64.urlsafe_b64decode(key))
        with tarfile.open(fileobj=writer, mode="w|") as tar:
            raw = json.dumps(_manifest(includes_state=True)).encode()
            _tar_file(tar, "manifest.json", io.BytesIO(raw), len(raw))
            _tar_file(tar, "database.dump", io.BytesIO(b"dump"), 4)
            payload = b"synthetic-encrypted-state"
            _tar_file(tar, f"state/{account_id}/state.enc", io.BytesIO(payload), len(payload))
        writer.finish()

    report = verify_archive(archive, key_file)
    assert report["state_accounts"] == 1
    assert report["restore_state_dir_required"] is True
    assert any("state-dir" in warning for warning in report["warnings"])
    with_state = verify_archive(archive, key_file, state_dir=tmp_path / "target")
    assert all("state-dir" not in warning for warning in with_state["warnings"])


@pytest.mark.parametrize("failure", ["wrong_key", "tampered", "truncated", "not_a_backup"])
def test_verify_rejects_unauthenticated_or_malformed_archives(tmp_path, failure):
    from tg_control.backup import verify_archive

    key = Fernet.generate_key()
    key_file = secret(tmp_path / "key", key)
    archive = _synthetic_archive(tmp_path / "archive.tgcbak", key, _manifest())
    if failure == "wrong_key":
        key_file = secret(tmp_path / "wrong", Fernet.generate_key())
    elif failure == "tampered":
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0xFF
        archive.write_bytes(bytes(data))
    elif failure == "truncated":
        archive.write_bytes(archive.read_bytes()[:-24])
    else:
        archive.write_bytes(b"not an encrypted backup at all")
    with pytest.raises(BackupError):
        verify_archive(archive, key_file)


def test_verify_rejects_invalid_layout_and_dump(tmp_path):
    from tg_control.backup import verify_archive

    key = Fernet.generate_key()
    key_file = secret(tmp_path / "key", key)
    empty = _synthetic_archive(tmp_path / "empty.tgcbak", key, _manifest(), dump=b"")
    with pytest.raises(BackupError, match="empty database dump"):
        verify_archive(empty, key_file)
    inconsistent = _synthetic_archive(
        tmp_path / "inconsistent.tgcbak", key, _manifest(includes_state=True)
    )
    with pytest.raises(BackupError, match="invalid backup contents"):
        verify_archive(inconsistent, key_file)


def test_verify_cli_prints_report_and_fails_on_incompatibility(tmp_path, monkeypatch, capsys):
    from tg_control.backup import main

    key = Fernet.generate_key()
    key_file = secret(tmp_path / "key", key)
    archive = _synthetic_archive(tmp_path / "good.tgcbak", key, _manifest())
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    base = [
        "tg-control-backup",
        "verify",
        "--input",
        str(archive),
        "--backup-key-file",
        str(key_file),
        "--scratch-dir",
        str(scratch),
    ]
    monkeypatch.setattr(sys, "argv", [*base, "--json"])
    main()
    assert '"integrity": "verified"' in capsys.readouterr().out

    monkeypatch.setattr(
        sys,
        "argv",
        [*base, "--expected-schema-revision", "0009_other"],
    )
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 3
    printed = capsys.readouterr().out
    assert "Warning" in printed
    assert list(scratch.iterdir()) == []
