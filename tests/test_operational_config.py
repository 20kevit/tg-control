import os
import subprocess
import sys
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]


def test_synthetic_secret_initialization_never_overwrites_or_prints_values(tmp_path):
    directory = tmp_path / "private"
    command = [
        sys.executable,
        str(ROOT / "scripts/init_synthetic_secrets.py"),
        "--directory",
        str(directory),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    names = [
        "postgres_password",
        "database_url",
        "backup_database_url",
        "owner_token",
        "encryption_key",
        "backup_key",
    ]
    contents = {name: (directory / name).read_text().strip() for name in names}
    assert all(
        value not in result.stdout and value not in result.stderr for value in contents.values()
    )
    assert contents["postgres_password"] in contents["database_url"]
    assert contents["postgres_password"] in contents["backup_database_url"]
    assert "127.0.0.1:55432" in contents["backup_database_url"]
    assert contents["encryption_key"] != contents["backup_key"]
    Fernet(contents["encryption_key"].encode())
    Fernet(contents["backup_key"].encode())
    if os.name == "posix":
        assert directory.stat().st_mode & 0o077 == 0
        assert all((directory / name).stat().st_mode & 0o077 == 0 for name in names)
    second = subprocess.run(command, capture_output=True, text=True, check=False)
    assert second.returncode != 0
    assert all((directory / name).read_text().strip() == contents[name] for name in names)
    assert all(
        value not in second.stdout and value not in second.stderr for value in contents.values()
    )


def test_compose_security_posture_is_local_only():
    import yaml

    config = yaml.safe_load((ROOT / "compose.yaml").read_text())
    gateway = config["services"]["gateway"]
    database = config["services"]["db"]
    assert gateway["ports"] == ["127.0.0.1:8000:8000"]
    assert "ports" not in database
    assert gateway["read_only"] is True
    assert "ALL" in gateway["cap_drop"] and "ALL" in database["cap_drop"]
    assert "no-new-privileges:true" in gateway["security_opt"]
    assert "no-new-privileges:true" in database["security_opt"]
    assert all(value["file"].startswith("./secrets/") for value in config["secrets"].values())
    assert gateway["environment"]["TG_SYNTHETIC_STATE_DIR"]
    # Hardened single-instance runtime properties introduced with the deployment
    # foundation: unprivileged user, no extra capabilities, bounded resources.
    assert gateway["user"] == "${TG_UID:-10001}:${TG_GID:-10001}"
    assert gateway["init"] is True
    assert gateway["mem_limit"] and gateway["pids_limit"] and gateway["cpus"]
    assert int(gateway["stop_grace_period"].rstrip("s")) >= 35
    assert any("noexec" in mount for mount in gateway["tmpfs"])
    assert gateway["logging"]["options"]["max-size"]
    assert gateway["depends_on"]["db"]["condition"] == "service_healthy"
    migrate = config["services"]["migrate"]
    assert migrate["profiles"] == ["tools"] and migrate["command"] == ["upgrade", "head"]
    override = yaml.safe_load((ROOT / "compose.backup.yaml").read_text())
    assert override["services"]["db"]["ports"] == ["127.0.0.1:55432:5432"]
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "--require-hashes" in dockerfile and "--workers" in dockerfile and '"1"' in dockerfile
    assert "tg_control.healthcheck" in dockerfile
    assert "USER ${APP_UID}:${APP_GID}" in dockerfile
    assert "--timeout-graceful-shutdown" in dockerfile
    assert "secrets" in (ROOT / ".dockerignore").read_text()


def test_secret_setup_refuses_a_single_existing_target_without_partial_writes(tmp_path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    existing = directory / "backup_database_url"
    existing.write_text("existing synthetic value")
    existing.chmod(0o600)
    command = [
        sys.executable,
        str(ROOT / "scripts/init_synthetic_secrets.py"),
        "--directory",
        str(directory),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    assert result.returncode != 0
    assert [path.name for path in directory.iterdir()] == ["backup_database_url"]
    assert existing.read_text() == "existing synthetic value"


def test_healthcheck_invalid_config_fails_without_disclosing_file_contents(tmp_path):
    token = "synthetic-owner-token-" + "x" * 32
    token_file = tmp_path / "owner"
    token_file.write_text(token)
    token_file.chmod(0o600)
    env = {**os.environ, "TG_OWNER_TOKEN_FILE": str(token_file), "TG_HEALTH_PORT": "not-a-port"}
    result = subprocess.run(
        [sys.executable, "-m", "tg_control.healthcheck"], env=env, capture_output=True, text=True
    )
    assert result.returncode == 1 and result.stderr.strip() == "health check failed"
    assert token not in result.stdout + result.stderr and str(token_file) not in result.stderr
