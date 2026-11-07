"""Offline, authenticated-encrypted PostgreSQL + synthetic-state backup/restore.

The gateway must be stopped. Restore deliberately requires a new empty target.
No Telegram sessions or owner/client credentials are created by this tool.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import psycopg
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

MAGIC = b"TGCBACK1"
CHUNK = 1024 * 1024
ALLOWED_CONNECTION_FIELDS = {
    "host",
    "port",
    "user",
    "password",
    "dbname",
    "sslmode",
    "sslrootcert",
    "sslcert",
    "sslkey",
    "connect_timeout",
    "application_name",
}


APP_VERSION = "0.1.0"


class BackupError(Exception):
    pass


def _conninfo(connection: dict[str, str]) -> str:
    """Build a libpq DSN in memory. It is never logged, printed or serialised."""
    parts = []
    for key, value in connection.items():
        escaped = value.replace("\\", "\\\\").replace("'", "\\'")
        parts.append(f"{key}='{escaped}'")
    return " ".join(parts)


def _private_file(path: Path) -> bytes:
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or (os.name == "posix" and info.st_mode & 0o077):
            raise BackupError("secret file must be regular and private (mode 0600)")
        value = stream.read(8193).strip()
    if not value or len(value) > 8192:
        raise BackupError("invalid secret file")
    return value


def _backup_key(path: Path) -> bytes:
    try:
        key = base64.b64decode(_private_file(path), altchars=b"-_", validate=True)
    except (ValueError, TypeError):
        raise BackupError("invalid backup key") from None
    if len(key) != 32:
        raise BackupError("backup key must encode 32 random bytes")
    return key


def _connection(path: Path) -> dict[str, str]:
    try:
        url = make_url(_private_file(path).decode("utf-8"))
        if url.get_backend_name() != "postgresql":
            raise ValueError
        values = {
            "host": url.host,
            "port": url.port,
            "user": url.username,
            "password": url.password,
            "dbname": url.database,
        }
        values.update(url.query)
        if set(values) - ALLOWED_CONNECTION_FIELDS:
            raise ValueError
        result = {}
        for key, value in values.items():
            if value is None:
                continue
            if not isinstance(value, (str, int)) or any(c in str(value) for c in "\r\n\x00"):
                raise ValueError
            result[key] = str(value)
        if not result.get("dbname"):
            raise ValueError
        return result
    except (UnicodeError, ValueError, TypeError, ArgumentError):
        raise BackupError("invalid PostgreSQL connection file") from None


def _private_write(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)


@contextmanager
def _service(connection: dict[str, str], scratch: Path):
    """Pass database credentials via a private libpq service file, never argv."""
    path = scratch / "pg_service.conf"
    text = "[tgcontrol]\n" + "".join(f"{key}={value}\n" for key, value in connection.items())
    _private_write(path, text.encode("utf-8"))
    # Do not pass application secrets, PG* overrides, Python injection, loader
    # overrides, or the caller's ~/.pgpass to child tools. Explicit TLS paths
    # and connection settings must come from the private database URL file.
    env = {
        "PATH": os.defpath,
        "HOME": str(scratch),
        "TMPDIR": str(scratch),
        "LANG": "C.UTF-8",
        "PGSERVICEFILE": str(path),
        "PGSERVICE": "tgcontrol",
    }
    yield env


def _program(name: str, bin_dir: Path | None) -> str:
    executable = str(bin_dir / name) if bin_dir else shutil.which(name)
    if not executable or not os.path.isfile(executable) or not os.access(executable, os.X_OK):
        raise BackupError(f"required PostgreSQL tool is unavailable: {name}")
    return executable


@contextmanager
def _state_lock(root: Path | None):
    if root is None:
        yield
        return
    if os.name != "posix":
        raise BackupError("synthetic state backup requires POSIX file locking")
    import fcntl

    if not root.is_dir() or root.is_symlink() or root.stat().st_mode & 0o077:
        raise BackupError("synthetic state directory must exist and be private")
    fd = os.open(root / ".lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise BackupError("stop the gateway before backing up its state") from None
        yield
    finally:
        os.close(fd)


def _state_files(root: Path):
    for directory in sorted(root.iterdir()):
        if directory.name == ".lock":
            continue
        if directory.is_symlink() or not directory.is_dir():
            raise BackupError("unexpected synthetic state entry")
        try:
            if str(UUID(directory.name)) != directory.name:
                raise ValueError
        except ValueError:
            raise BackupError("unexpected synthetic account directory") from None
        if os.name == "posix" and directory.stat().st_mode & 0o077:
            raise BackupError("synthetic account directory must be private")
        path = directory / "state.enc"
        if path.is_symlink() or not path.is_file():
            raise BackupError("invalid synthetic account state")
        yield directory.name, path


class _EncryptWriter:
    def __init__(self, stream, key: bytes):
        self.stream = stream
        self.header = MAGIC + secrets.token_bytes(12)
        self.stream.write(self.header)
        self.encryptor = Cipher(
            algorithms.AES(key), modes.GCM(self.header[len(MAGIC) :])
        ).encryptor()
        self.encryptor.authenticate_additional_data(self.header)

    def write(self, data: bytes) -> int:
        self.stream.write(self.encryptor.update(data))
        return len(data)

    def finish(self) -> None:
        self.stream.write(self.encryptor.finalize())
        self.stream.write(self.encryptor.tag)
        self.stream.flush()
        os.fsync(self.stream.fileno())


def _source_revision(connection: dict[str, str]) -> str | None:
    """Read the Alembic revision of the dumped database without printing secrets."""
    try:
        with psycopg.connect(_conninfo(connection), connect_timeout=5) as db:
            row = db.execute("SELECT version_num FROM alembic_version").fetchone()
    except psycopg.Error:
        return None
    if not row or not isinstance(row[0], str) or len(row[0]) > 32:
        return None
    return row[0]


SUPPORTED_MANIFEST_VERSIONS = (1, 2)


def _parse_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw)
    except (ValueError, UnicodeError):
        raise BackupError("invalid backup manifest") from None
    if not isinstance(manifest, dict) or type(manifest.get("version")) is not int:
        raise BackupError("unsupported backup manifest")
    version = manifest["version"]
    if version not in SUPPORTED_MANIFEST_VERSIONS:
        raise BackupError("unsupported backup manifest")
    if not isinstance(manifest.get("includes_state"), bool):
        raise BackupError("invalid backup manifest")
    allowed = {"version", "includes_state"}
    if version == 2:
        allowed |= {"app_version", "schema_revision", "created_at"}
        if not isinstance(manifest.get("app_version"), str) or len(manifest["app_version"]) > 32:
            raise BackupError("invalid backup manifest")
        revision = manifest.get("schema_revision")
        if revision is not None and (
            not isinstance(revision, str) or not re.fullmatch(r"[0-9A-Za-z_]{1,32}", revision)
        ):
            raise BackupError("invalid backup manifest")
        if not isinstance(manifest.get("created_at"), str) or len(manifest["created_at"]) > 40:
            raise BackupError("invalid backup manifest")
    if set(manifest) != allowed:
        raise BackupError("unsupported backup manifest")
    return manifest


def verify_archive(
    archive: Path,
    key_file: Path,
    *,
    state_dir: Path | None = None,
    scratch_dir: Path | None = None,
    expected_schema_revision: str | None = None,
) -> dict:
    """Read-only integrity and compatibility check of an encrypted backup.

    Authenticates the ciphertext, validates the archive layout and manifest, checks
    that the dump is non-empty and that state members are canonical, and compares
    the recorded schema revision with the running application when supplied. It
    never writes to the restore targets and never prints key material.
    """
    key = _backup_key(key_file)
    with tempfile.TemporaryDirectory(prefix="tg-control-verify-", dir=scratch_dir) as temp:
        scratch = Path(temp)
        plaintext = scratch / "archive.tar"
        _decrypt(archive, plaintext, key)
        try:
            with tarfile.open(plaintext, mode="r:") as tar:
                members = tar.getmembers()
                names = [member.name for member in members]
                if len(set(names)) != len(names) or any(not member.isfile() for member in members):
                    raise BackupError("invalid backup contents")
                if {"manifest.json", "database.dump"} - set(names):
                    raise BackupError("invalid backup contents")
                manifest_member = tar.getmember("manifest.json")
                if manifest_member.size > 4096:
                    raise BackupError("invalid backup manifest")
                manifest_file = tar.extractfile(manifest_member)
                if manifest_file is None:
                    raise BackupError("invalid backup manifest")
                manifest = _parse_manifest(manifest_file.read())
                dump = tar.getmember("database.dump")
                if dump.size <= 0:
                    raise BackupError("empty database dump")
                core_members = {"manifest.json", "database.dump"}
                state_members = [name for name in names if name not in core_members]
                for name in state_members:
                    parts = name.split("/")
                    if len(parts) != 3 or parts[0] != "state" or parts[2] != "state.enc":
                        raise BackupError("invalid backup contents")
                    try:
                        if str(UUID(parts[1])) != parts[1]:
                            raise ValueError
                    except ValueError:
                        raise BackupError("invalid backup contents") from None
                if bool(state_members) != manifest["includes_state"]:
                    raise BackupError("invalid backup contents")
        except tarfile.TarError:
            raise BackupError("invalid backup contents") from None
        report = {
            "format": "tg-control-encrypted-backup",
            "manifest_version": manifest["version"],
            "app_version": manifest.get("app_version"),
            "schema_revision": manifest.get("schema_revision"),
            "created_at": manifest.get("created_at"),
            "includes_state": manifest["includes_state"],
            "state_accounts": len([name for name in names if name.startswith("state/")]),
            "dump_bytes": dump.size,
            "archive_bytes": archive.stat().st_size,
            "integrity": "verified",
            "restore_state_dir_required": manifest["includes_state"],
            "compatible": True,
            "warnings": [],
        }
        if manifest["version"] == 1:
            report["warnings"].append(
                "manifest v1: schema revision and app version were not recorded"
            )
        if expected_schema_revision is not None:
            recorded = manifest.get("schema_revision")
            if recorded is None:
                report["warnings"].append("schema revision unknown; migration check skipped")
            elif recorded != expected_schema_revision:
                report["compatible"] = False
                report["warnings"].append(
                    "backup schema revision differs from the running application"
                )
        if state_dir is None and manifest["includes_state"]:
            report["warnings"].append(
                "archive includes synthetic state; a new --state-dir is required"
            )
        return report


def _tar_file(archive: tarfile.TarFile, name: str, stream, size: int) -> None:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o600
    info.uid = info.gid = 0
    info.mtime = 0
    archive.addfile(info, stream)


def create_backup(
    database_file: Path,
    key_file: Path,
    output: Path,
    *,
    state_dir: Path | None,
    scratch_dir: Path | None = None,
    bin_dir: Path | None = None,
) -> None:
    key, connection = _backup_key(key_file), _connection(database_file)
    pg_dump = _program("pg_dump", bin_dir)
    with (
        _state_lock(state_dir),
        tempfile.TemporaryDirectory(prefix="tg-control-", dir=scratch_dir) as temp,
    ):
        scratch = Path(temp)
        dump = scratch / "database.dump"
        with _service(connection, scratch) as env:
            fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as target:
                result = subprocess.run(
                    [pg_dump, "--format=custom", "--no-owner", "--no-privileges"],
                    env=env,
                    stdout=target,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            if result.returncode:
                raise BackupError("PostgreSQL backup failed; check server and tool compatibility")
        state_files = list(_state_files(state_dir)) if state_dir else []
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        # On failure leave the incomplete, encrypted file for owner inspection.
        # Unlinking by pathname could delete a different file substituted in a race.
        with os.fdopen(fd, "wb") as encrypted:
            writer = _EncryptWriter(encrypted, key)
            manifest = json.dumps(
                {
                    "version": 2,
                    "includes_state": state_dir is not None,
                    "app_version": APP_VERSION,
                    "schema_revision": _source_revision(connection),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            ).encode()
            with tarfile.open(fileobj=writer, mode="w|") as archive:
                _tar_file(archive, "manifest.json", io.BytesIO(manifest), len(manifest))
                with dump.open("rb") as file:
                    _tar_file(archive, "database.dump", file, dump.stat().st_size)
                for account_id, path in state_files:
                    state_fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                    with os.fdopen(state_fd, "rb") as file:
                        info = os.fstat(file.fileno())
                        if not stat.S_ISREG(info.st_mode):
                            raise BackupError("invalid synthetic account state")
                        _tar_file(archive, f"state/{account_id}/state.enc", file, info.st_size)
            writer.finish()


def _decrypt(archive: Path, output: Path, key: bytes) -> None:
    with archive.open("rb") as source:
        header = source.read(len(MAGIC) + 12)
        if len(header) != len(MAGIC) + 12 or not header.startswith(MAGIC):
            raise BackupError("invalid backup format")
        size = os.fstat(source.fileno()).st_size
        remaining = size - len(header) - 16
        if remaining < 0:
            raise BackupError("invalid backup format")
        source.seek(-16, os.SEEK_END)
        tag = source.read(16)
        source.seek(len(header))
        decryptor = Cipher(algorithms.AES(key), modes.GCM(header[len(MAGIC) :], tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb") as target:
                while remaining:
                    data = source.read(min(CHUNK, remaining))
                    if not data:
                        raise BackupError("truncated backup")
                    remaining -= len(data)
                    target.write(decryptor.update(data))
                target.write(decryptor.finalize())
        except InvalidTag:
            raise BackupError("backup authentication failed") from None


def _empty_database(env: dict[str, str]) -> bool:
    # A separate isolated process ensures the safety check and pg_restore use
    # exactly the same libpq service file and cannot inherit ambient PG* values.
    result = subprocess.run(
        [sys.executable, "-I", "-m", "tg_control.backup_probe"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode == 0:
        return True
    if result.returncode == 2:
        return False
    raise BackupError("restore target could not be verified")


def restore_backup(
    database_file: Path,
    key_file: Path,
    archive: Path,
    *,
    state_dir: Path | None,
    confirm_empty: bool,
    scratch_dir: Path | None = None,
    bin_dir: Path | None = None,
) -> None:
    if not confirm_empty:
        raise BackupError("explicit confirmation of a new empty restore target is required")
    key, connection = _backup_key(key_file), _connection(database_file)
    pg_restore = _program("pg_restore", bin_dir)
    if state_dir and state_dir.exists():
        raise BackupError("restore state directory must not already exist")
    with tempfile.TemporaryDirectory(prefix="tg-control-", dir=scratch_dir) as temp:
        scratch = Path(temp)
        plaintext = scratch / "archive.tar"
        _decrypt(archive, plaintext, key)  # Verify the authentication tag before parsing anything.
        with tarfile.open(plaintext, mode="r:") as tar:
            members = tar.getmembers()
            names = [member.name for member in members]
            if len(set(names)) != len(names) or any(not member.isfile() for member in members):
                raise BackupError("invalid backup contents")
            if "manifest.json" not in names or "database.dump" not in names:
                raise BackupError("invalid backup contents")
            manifest_member = tar.getmember("manifest.json")
            if manifest_member.size > 4096:
                raise BackupError("invalid backup manifest")
            manifest_file = tar.extractfile(manifest_member)
            if manifest_file is None:
                raise BackupError("invalid backup manifest")
            manifest = _parse_manifest(manifest_file.read())
            if manifest["includes_state"] != (state_dir is not None):
                raise BackupError(
                    "restore must specify a new state directory exactly when included"
                )
            state_members = []
            for member in members:
                if member.name in {"manifest.json", "database.dump"}:
                    continue
                parts = member.name.split("/")
                try:
                    valid = (
                        len(parts) == 3
                        and parts[0] == "state"
                        and parts[2] == "state.enc"
                        and str(UUID(parts[1])) == parts[1]
                        and manifest["includes_state"]
                    )
                except ValueError:
                    valid = False
                if not valid:
                    raise BackupError("invalid backup path")
                state_members.append((parts[1], member))
            with _service(connection, scratch) as env:
                if not _empty_database(env):
                    raise BackupError("restore database is not empty")
                dump = scratch / "database.dump"
                content = tar.extractfile("database.dump")
                if content is None:
                    raise BackupError("invalid database backup")
                fd = os.open(dump, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as target:
                    shutil.copyfileobj(content, target, CHUNK)
                if state_dir:
                    # Exclusive create; never overwrite an existing directory.
                    state_dir.mkdir(mode=0o700)
                result = subprocess.run(
                    [
                        pg_restore,
                        "--single-transaction",
                        "--exit-on-error",
                        "--no-owner",
                        "--no-privileges",
                        "--dbname=service=tgcontrol",
                        str(dump),
                    ],
                    env=env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            if result.returncode:
                raise BackupError("PostgreSQL restore failed; target may require inspection")
            for account_id, member in state_members:
                directory = state_dir / account_id
                directory.mkdir(mode=0o700)
                content = tar.extractfile(member)
                if content is None:
                    raise BackupError("invalid synthetic state backup")
                fd = os.open(directory / "state.enc", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as target:
                    shutil.copyfileobj(content, target, CHUNK)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Encrypted synthetic gateway backup; stop the gateway first"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("create", "restore", "verify"):
        item = sub.add_parser(command)
        item.add_argument(
            "--database-url-file", type=Path, required=command != "verify"
        )
        item.add_argument("--backup-key-file", type=Path, required=True)
        item.add_argument("--state-dir", type=Path)
        item.add_argument("--scratch-dir", type=Path, help="private tmpfs recommended")
        item.add_argument("--pg-bin-dir", type=Path)
        if command == "create":
            item.add_argument("--output", type=Path, required=True)
        elif command == "restore":
            item.add_argument("--input", type=Path, required=True)
            item.add_argument("--confirm-empty-target", action="store_true")
        else:
            item.add_argument("--input", type=Path, required=True)
            item.add_argument(
                "--expected-schema-revision",
                default=None,
                help="fail when the recorded migration revision differs",
            )
            item.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args()
    try:
        if args.command == "create":
            create_backup(
                args.database_url_file,
                args.backup_key_file,
                args.output,
                state_dir=args.state_dir,
                scratch_dir=args.scratch_dir,
                bin_dir=args.pg_bin_dir,
            )
            print("Encrypted backup created.")
        elif args.command == "verify":
            report = verify_archive(
                args.input,
                args.backup_key_file,
                state_dir=args.state_dir,
                scratch_dir=args.scratch_dir,
                expected_schema_revision=args.expected_schema_revision,
            )
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print(
                    "Backup verified: "
                    f"manifest v{report['manifest_version']}, "
                    f"schema {report['schema_revision'] or 'unknown'}, "
                    f"{report['state_accounts']} state account(s)."
                )
                for warning in report["warnings"]:
                    print(f"Warning: {warning}")
            if not report["compatible"]:
                raise SystemExit(3)
        else:
            restore_backup(
                args.database_url_file,
                args.backup_key_file,
                args.input,
                state_dir=args.state_dir,
                confirm_empty=args.confirm_empty_target,
                scratch_dir=args.scratch_dir,
                bin_dir=args.pg_bin_dir,
            )
            print("Backup restored to empty targets.")
    except BackupError as exc:
        # BackupError messages are fixed, content-free diagnostics.
        print(f"Backup operation failed: {exc}.", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, psycopg.Error, tarfile.TarError):
        # Never print raw exceptions, which may contain connection information or content.
        print(
            "Backup operation failed; see the operations guide and inspect targets safely.",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
