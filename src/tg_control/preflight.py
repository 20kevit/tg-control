"""Read-only deployment preflight.

Runs the same checks an operator performs by hand before starting the gateway,
without binding a port, migrating, writing application state or printing any
secret value: configuration resolution, secret-file privacy, writable private
state directories, disk headroom, backup-key sanity, database connectivity and
the schema revision gate.

Usage:
    tg-control-preflight [--json] [--min-free-mb 1024]

Exit codes (matching the startup contract):
    0  all checks passed (warnings may be present)
    1  configuration problem
    2  filesystem/permission problem
    3  database or schema problem
    4  unexpected internal failure
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import stat
import sys
from pathlib import Path

from cryptography.fernet import Fernet

from .config import Settings
from .db import EXPECTED_SCHEMA_REVISION, make_engine, schema_state
from .version import __version__

SECRET_VARIABLES = (
    "TG_OWNER_TOKEN_FILE",
    "TG_ENCRYPTION_KEY_FILE",
    "TG_DATABASE_URL_FILE",
)
OPTIONAL_SECRET_VARIABLES = ("TG_BACKUP_KEY_FILE",)
DIRECTORY_VARIABLES = ("TG_SYNTHETIC_STATE_DIR", "TG_MEDIA_STAGING_DIR")
MIN_FREE_MB = 1024


class Report:
    def __init__(self) -> None:
        self.checks: list[dict[str, str]] = []
        self._failure_categories: set[str] = set()

    def add(self, name: str, status: str, detail: str) -> None:
        # Details are fixed vocabulary produced here; never a secret value.
        self.checks.append({"name": name, "status": status, "detail": detail})

    def fail(self, name: str, category: str, detail: str) -> None:
        self.checks.append({"name": name, "status": "fail", "detail": detail, "category": category})
        self._failure_categories.add(category)

    @property
    def failed(self) -> list[dict[str, str]]:
        return [check for check in self.checks if check["status"] == "fail"]

    @property
    def warnings(self) -> list[dict[str, str]]:
        return [check for check in self.checks if check["status"] == "warn"]

    def exit_code(self) -> int:
        if not self._failure_categories:
            return 0
        for code, category in ((1, "config"), (2, "storage"), (3, "database")):
            if category in self._failure_categories:
                return code
        return 4


def _add_failure(report: Report, name: str, category: str, detail: str) -> None:
    report.fail(name, category, detail)


def _file_privacy(path: Path) -> str | None:
    """Return a normalized problem code, never the path or its contents."""
    try:
        info = path.stat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if not stat.S_ISREG(info.st_mode):
        return "not_a_regular_file"
    if os.name == "posix" and info.st_mode & 0o077:
        return "group_or_world_accessible"
    if info.st_size == 0:
        return "empty"
    return None


def _directory_privacy(path: Path) -> str | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if not stat.S_ISDIR(info.st_mode):
        return "not_a_directory"
    if os.name == "posix" and info.st_mode & 0o077:
        return "group_or_world_accessible"
    if not os.access(path, os.W_OK | os.X_OK):
        return "not_writable_by_current_user"
    return None


def _check_secret_files(report: Report) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for variable in SECRET_VARIABLES:
        raw = os.environ.get(variable)
        if not raw:
            if variable == "TG_DATABASE_URL_FILE" and os.environ.get("TG_DATABASE_URL"):
                report.add(variable, "pass", "inline database URL configured")
                continue
            _add_failure(report, variable, "config", "not set")
            continue
        problem = _file_privacy(Path(raw))
        if problem:
            _add_failure(report, variable, "config", problem)
        else:
            resolved[variable] = raw
            report.add(variable, "pass", "private regular file")
    for variable in OPTIONAL_SECRET_VARIABLES:
        raw = os.environ.get(variable)
        if not raw:
            report.add(variable, "warn", "not configured; encrypted backups unavailable")
            continue
        problem = _file_privacy(Path(raw))
        if problem:
            _add_failure(report, variable, "config", problem)
        else:
            resolved[variable] = raw
            report.add(variable, "pass", "private regular file")
    return resolved


def _check_backup_key(report: Report, resolved: dict[str, str], settings: Settings | None) -> None:
    raw = resolved.get("TG_BACKUP_KEY_FILE")
    if raw is None or settings is None:
        return
    try:
        key = Path(raw).read_bytes().strip()
        Fernet(key)  # raises on malformed key material
    except Exception:
        _add_failure(report, "backup_key", "config", "invalid Fernet key")
        return
    if key == settings.encryption_key:
        # Reusing one key for live state and archives means one compromise or one
        # lost file destroys both; this is a warning, not a hard stop.
        report.checks.append(
            {
                "name": "backup_key",
                "status": "warn",
                "detail": "backup key must differ from the encryption key",
            }
        )
    else:
        report.add("backup_key", "pass", "independent private key")


def _check_directories(report: Report, min_free_mb: int) -> None:
    for variable in DIRECTORY_VARIABLES:
        raw = os.environ.get(variable)
        if not raw:
            report.add(variable, "pass", "not configured")
            continue
        path = Path(raw)
        problem = _directory_privacy(path)
        if problem:
            _add_failure(report, variable, "storage", problem)
            continue
        try:
            usage = shutil.disk_usage(path)
        except OSError:
            _add_failure(report, variable, "storage", "disk usage unavailable")
            continue
        free_mb = usage.free // (1024 * 1024)
        if free_mb < min_free_mb:
            _add_failure(report, variable, "storage", f"less than {min_free_mb} MiB free")
        else:
            report.add(variable, "pass", f"private, writable, {free_mb} MiB free")


def _check_database(report: Report, settings: Settings) -> None:
    engine = make_engine(settings.database_url)
    try:
        state = schema_state(engine)
    except Exception:
        _add_failure(report, "database", "database", "connection failed")
        return
    finally:
        engine.dispose()
    if state["state"] == "unavailable":
        _add_failure(report, "database", "database", "connection failed")
        return
    report.add("database", "pass", "reachable")
    if state["state"] == "current" and state["objects"] == "complete":
        report.add("schema", "pass", f"revision {EXPECTED_SCHEMA_REVISION}")
        return
    detail = state["state"]
    if state["state"] == "current":
        detail = "incomplete"
    _add_failure(report, "schema", "database", detail)


def run(min_free_mb: int = MIN_FREE_MB) -> Report:
    report = Report()
    resolved = _check_secret_files(report)
    settings: Settings | None = None
    try:
        settings = Settings.from_env()
    except (RuntimeError, ValueError) as exc:
        message = str(exc)
        if not message.startswith("missing required configuration"):
            # Validation errors from Settings are fixed strings; keep them but
            # never append the underlying value.
            message = "invalid configuration: " + message
        _add_failure(report, "settings", "config", message)
    else:
        report.add("settings", "pass", "validated")
    _check_directories(report, min_free_mb)
    _check_backup_key(report, resolved, settings)
    if settings is not None:
        _check_database(report, settings)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only deployment preflight")
    parser.add_argument("--json", action="store_true", help="emit a JSON report")
    parser.add_argument("--min-free-mb", type=int, default=MIN_FREE_MB)
    args = parser.parse_args(argv)
    try:
        report = run(min_free_mb=max(0, args.min_free_mb))
    except Exception:
        # Never print exception text: it can embed paths or connection strings.
        print(json.dumps({"ok": False, "error": "preflight failed"}))
        return 4
    code = report.exit_code()
    payload = {
        "ok": code == 0,
        "version": __version__,
        "schema_revision": EXPECTED_SCHEMA_REVISION,
        "checks": report.checks,
        "warnings": len(report.warnings),
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        for check in report.checks:
            print(f"[{check['status'].upper():4}] {check['name']}: {check['detail']}")
        print("preflight ok" if code == 0 else "preflight failed")
    return code


if __name__ == "__main__":
    sys.exit(main())
