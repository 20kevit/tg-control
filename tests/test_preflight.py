"""Deployment preflight: read-only, actionable, never leaks secret material."""

import json
import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "synthetic-preflight-owner-token-" + "p" * 24


def alembic(url: str, revision: str = "head", command: str = "upgrade") -> None:
    env = os.environ.copy()
    env["TG_DATABASE_URL"] = url
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def secret(path: Path, value: str, mode: int = 0o600) -> Path:
    path.write_text(value)
    path.chmod(mode)
    return path


def environment(tmp_path: Path, *, with_database: bool = True) -> dict[str, str]:
    database_url = f"sqlite:///{tmp_path / 'preflight.sqlite'}"
    if with_database:
        alembic(database_url)
    key = Fernet.generate_key().decode()
    return {
        "PATH": os.defpath,
        "HOME": str(tmp_path),
        "PYTHONPATH": str(ROOT / "src"),
        "TG_DATABASE_URL_FILE": str(secret(tmp_path / "database_url", database_url)),
        "TG_OWNER_TOKEN_FILE": str(secret(tmp_path / "owner_token", TOKEN)),
        "TG_ENCRYPTION_KEY_FILE": str(secret(tmp_path / "encryption_key", key)),
    }, key


def run_preflight(env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "tg_control.preflight", "--json", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_preflight_passes_on_a_valid_private_deployment(tmp_path):
    env, key = environment(tmp_path)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    env["TG_SYNTHETIC_STATE_DIR"] = str(state)
    backup_key = secret(tmp_path / "backup_key", Fernet.generate_key().decode())
    env["TG_BACKUP_KEY_FILE"] = str(backup_key)
    result = run_preflight(env)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True
    assert report["version"] and report["schema_revision"] == "0007"
    checks = {check["name"]: check["status"] for check in report["checks"]}
    assert checks["settings"] == "pass"
    assert checks["database"] == "pass" and checks["schema"] == "pass"
    assert checks["TG_OWNER_TOKEN_FILE"] == "pass"
    assert checks["TG_SYNTHETIC_STATE_DIR"] == "pass"
    # The report never echoes secret material or resolved paths.
    assert TOKEN not in result.stdout
    assert key not in result.stdout
    assert str(tmp_path) not in result.stdout


def test_missing_configuration_fails_with_variable_names_only(tmp_path):
    env = {"PATH": os.defpath, "HOME": str(tmp_path), "PYTHONPATH": str(ROOT / "src")}
    result = run_preflight(env)
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["ok"] is False
    text = json.dumps(report)
    assert "missing required configuration" in text
    assert "TG_OWNER_TOKEN_FILE" in text
    assert str(tmp_path) not in text


def test_group_readable_secret_file_fails_closed(tmp_path):
    env, _ = environment(tmp_path, with_database=False)
    Path(env["TG_OWNER_TOKEN_FILE"]).chmod(0o644)
    result = run_preflight(env)
    assert result.returncode == 1
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["TG_OWNER_TOKEN_FILE"]["status"] == "fail"
    assert checks["TG_OWNER_TOKEN_FILE"]["detail"] == "group_or_world_accessible"
    assert TOKEN not in result.stdout


def test_insecure_state_directory_is_a_storage_failure(tmp_path):
    env, _ = environment(tmp_path)
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o755)
    env["TG_SYNTHETIC_STATE_DIR"] = str(state)
    result = run_preflight(env)
    assert result.returncode == 2
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["TG_SYNTHETIC_STATE_DIR"]["detail"] == "group_or_world_accessible"


def test_unreachable_database_and_stale_schema_are_database_failures(tmp_path):
    env, _ = environment(tmp_path, with_database=False)
    unreachable = {**env, "TG_DATABASE_URL_FILE": str(secret(tmp_path / "bad_url", "sqlite:////nonexistent/dir/db.sqlite"))}
    result = run_preflight(unreachable)
    assert result.returncode == 3

    env, _ = environment(tmp_path)
    alembic(f"sqlite:///{tmp_path / 'preflight.sqlite'}", "0006", command="downgrade")
    result = run_preflight(env)
    assert result.returncode == 3
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["schema"]["status"] == "fail"
    assert checks["schema"]["detail"] in {"behind", "incomplete"}


def test_disk_headroom_check_and_duplicate_backup_key_warning(tmp_path):
    env, key = environment(tmp_path)
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    env["TG_SYNTHETIC_STATE_DIR"] = str(state)
    env["TG_BACKUP_KEY_FILE"] = str(secret(tmp_path / "backup_key", key))
    result = run_preflight(env)
    assert result.returncode == 0
    report = json.loads(result.stdout)
    checks = {check["name"]: check for check in report["checks"]}
    assert checks["backup_key"]["status"] == "warn"
    assert "must differ" in checks["backup_key"]["detail"]
    assert report["warnings"] >= 1

    too_small = run_preflight(env, "--min-free-mb", "100000000")
    assert too_small.returncode == 2
    checks = {check["name"]: check for check in json.loads(too_small.stdout)["checks"]}
    assert checks["TG_SYNTHETIC_STATE_DIR"]["status"] == "fail"


def test_configuration_failure_takes_precedence_over_storage(tmp_path):
    env, _ = environment(tmp_path)
    Path(env["TG_ENCRYPTION_KEY_FILE"]).chmod(0o644)  # config failure
    state = tmp_path / "state"
    state.mkdir()
    state.chmod(0o755)  # storage failure too
    env["TG_SYNTHETIC_STATE_DIR"] = str(state)
    result = run_preflight(env)
    assert result.returncode == 1


def test_unexpected_internal_failure_reports_only_the_exception_type(monkeypatch, capsys):
    """A hosted-only failure must be diagnosable without exposing data."""

    import tg_control.preflight as preflight

    def explode(*_args, **_kwargs):
        raise RuntimeError("postgresql://owner:secret@db/gateway")

    monkeypatch.setattr(preflight, "run", explode)
    assert preflight.main(["--json"]) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "error": "preflight failed",
        "error_type": "RuntimeError",
    }


def test_ephemeral_media_staging_uses_a_lower_headroom_threshold(tmp_path, monkeypatch):
    """Compose mounts media staging as a 256 MiB tmpfs; 1 GiB would always fail."""
    from types import SimpleNamespace

    import tg_control.preflight as preflight

    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    staging = tmp_path / "staging"
    staging.mkdir(mode=0o700)
    monkeypatch.setenv("TG_SYNTHETIC_STATE_DIR", str(state))
    monkeypatch.setenv("TG_MEDIA_STAGING_DIR", str(staging))
    monkeypatch.setattr(
        preflight.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(total=0, used=0, free=100 * 1024 * 1024),
    )
    report = preflight.Report()
    preflight._check_directories(report, preflight.MIN_FREE_MB)
    statuses = {check["name"]: check["status"] for check in report.checks}
    assert statuses["TG_SYNTHETIC_STATE_DIR"] == "fail"
    assert statuses["TG_MEDIA_STAGING_DIR"] == "pass"


def test_unmigrated_database_is_missing_not_unreachable(tmp_path):
    """A fresh database must be reported as unmigrated, not as a connection error."""
    env, _ = environment(tmp_path, with_database=False)
    result = run_preflight(env)
    assert result.returncode == 3
    checks = {check["name"]: check for check in json.loads(result.stdout)["checks"]}
    assert checks["database"]["status"] == "pass"
    assert checks["schema"]["status"] == "fail"
    assert checks["schema"]["detail"] == "missing"
