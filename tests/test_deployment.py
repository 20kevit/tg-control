"""Deployment artifacts must encode the documented security and lifecycle rules.

These are static checks: they prove the Dockerfile/Compose/CI configuration says
what the documentation claims. Runtime behavior is covered by
`tests/test_runtime_lifecycle.py` (host process) and recorded as BLOCKED for a
real container runtime in `docs/DEPLOYMENT.md`.
"""

import hashlib
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def dockerfile() -> str:
    return (ROOT / "Dockerfile").read_text()


def compose() -> dict:
    import yaml

    return yaml.safe_load((ROOT / "compose.yaml").read_text())


def test_dockerfile_is_multi_stage_non_root_and_secret_free():
    text = dockerfile()
    stages = re.findall(r"^FROM .*?(?: AS (\w+))?$", text, re.M)
    assert len(stages) >= 2, "multi-stage build expected"
    assert " AS builder" in text and " AS runtime" in text
    assert "USER ${APP_UID}:${APP_GID}" in text
    assert "ARG APP_UID=10001" in text and "ARG APP_GID=10001" in text
    assert "useradd --system" in text and "/usr/sbin/nologin" in text
    assert "GROUPADD" not in text
    # Dependencies are installed from the hash lock only, in the builder stage.
    assert "--require-hashes" in text
    runtime = text.split(" AS runtime", 1)[1]
    assert "pip install" not in runtime.split("COPY --from=builder")[0]
    # No secret or build-context wildcard copy.
    assert "COPY . " not in text and "COPY .\n" not in text
    instructions = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    ).upper()
    for forbidden in ("SECRET", "TOKEN=", "PASSWORD=", "PRIVATE KEY", "ENV TG_"):
        assert forbidden not in instructions, forbidden
    assert "HEALTHCHECK" in text and "tg_control.healthcheck" in text
    assert "STOPSIGNAL SIGTERM" in text
    assert "--timeout-graceful-shutdown" in text
    assert '"--workers", "1"' in text
    assert "EXPOSE 8000" in text and text.count("EXPOSE") == 1
    # Fixed writable state directories owned by the runtime user.
    assert "/var/lib/tg-control/synthetic" in text


def test_dockerignore_excludes_secrets_state_and_tooling():
    entries = {
        line.strip() for line in (ROOT / ".dockerignore").read_text().splitlines() if line.strip()
    }
    for required in (
        ".git",
        ".venv",
        "node_modules",
        "secrets",
        ".env",
        "*.key",
        "*.pem",
        "*.tgcbak",
        "state",
        "backups",
        "*.sqlite",
        "__pycache__",
    ):
        assert required in entries, required


def test_compose_is_loopback_only_without_docker_socket_or_privileges():
    config = compose()
    raw = (ROOT / "compose.yaml").read_text()
    assert "/var/run/docker.sock" not in raw
    assert "network_mode" not in raw
    gateway = config["services"]["gateway"]
    database = config["services"]["db"]
    assert gateway["ports"] == ["127.0.0.1:8000:8000"]
    assert "ports" not in database
    for service in (gateway, database):
        assert not service.get("privileged", False), "privileged mode is forbidden"
        assert service.get("pid", "container") != "host"
        assert "ALL" in service["cap_drop"]
        assert "no-new-privileges:true" in service["security_opt"]
    assert gateway["read_only"] is True
    assert gateway["init"] is True
    assert gateway["user"] == "${TG_UID:-10001}:${TG_GID:-10001}"
    assert gateway["depends_on"]["db"]["condition"] == "service_healthy"
    assert database["healthcheck"]["test"][0] == "CMD-SHELL"
    assert "pg_isready" in " ".join(database["healthcheck"]["test"])
    assert gateway["healthcheck"]["test"] == ["CMD", "python", "-m", "tg_control.healthcheck"]
    assert gateway["restart"] == "unless-stopped" and database["restart"] == "unless-stopped"
    for service in (gateway, database):
        assert service["logging"]["options"]["max-size"]
        assert service["logging"]["options"]["max-file"]


def test_compose_declares_resource_limits_log_rotation_and_graceful_stop():
    config = compose()
    gateway = config["services"]["gateway"]
    database = config["services"]["db"]
    assert gateway["mem_limit"] and gateway["cpus"] and gateway["pids_limit"]
    assert database["mem_limit"] and database["pids_limit"] and database["shm_size"]
    assert int(gateway["stop_grace_period"].rstrip("s")) >= 35
    assert int(database["stop_grace_period"].rstrip("s")) >= 30
    assert any("size=" in mount for mount in gateway["tmpfs"])
    assert any("noexec" in mount for mount in gateway["tmpfs"])


def test_compose_secrets_are_mounted_files_and_environment_has_no_values():
    config = compose()
    gateway = config["services"]["gateway"]
    assert set(gateway["secrets"]) == {"database_url", "owner_token", "encryption_key"}
    for name, entry in config["secrets"].items():
        assert entry["file"].startswith("./secrets/"), name
    for key, value in gateway["environment"].items():
        # Only *_FILE indirections may carry secret material; values are paths,
        # hostnames, origins or log levels, never a credential.
        assert key.endswith("_FILE") or key in {
            "TG_SYNTHETIC_STATE_DIR",
            "TG_MEDIA_STAGING_DIR",
            "TG_ALLOWED_HOSTS",
            "TG_ALLOWED_ORIGINS",
            "TG_LOG_LEVEL",
            "TG_HEALTH_PORT",
            "TMPDIR",
        }, key
        assert not re.search(r"[A-Za-z0-9_-]{40,}", str(value)), f"possible literal secret in {key}"


def test_migrations_are_an_explicit_one_shot_service_not_a_startup_action():
    config = compose()
    migrate = config["services"]["migrate"]
    assert migrate["profiles"] == ["tools"]
    assert migrate["command"] == ["upgrade", "head"]
    assert migrate["read_only"] is True
    assert "ALL" in migrate["cap_drop"]
    assert migrate["depends_on"]["db"]["condition"] == "service_healthy"
    # The gateway must not run migrations on start.
    gateway = config["services"]["gateway"]
    assert "alembic" not in json.dumps(gateway.get("command", []))
    assert "migrate" not in json.dumps(gateway.get("entrypoint", []))


def test_example_environment_contains_placeholders_only():
    text = (ROOT / ".env.example").read_text()
    assert "TG_OWNER_TOKEN=" not in text and "TG_ENCRYPTION_KEY=" not in text
    assert "TG_DATABASE_URL=" not in text
    assert "secrets/" in text
    allowed = {
        "TG_UID",
        "TG_GID",
        "TG_ALLOWED_HOSTS",
        "TG_ALLOWED_ORIGINS",
        "TG_LOG_LEVEL",
    }
    for line in text.splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        assert key in allowed, f"unexpected variable in .env.example: {key}"
        assert not re.search(r"[A-Za-z0-9+/=]{32,}", value), key


def test_hosted_ci_workflow_is_pinned_least_privilege_and_runs_the_local_gate():
    import yaml

    workflow_path = ROOT / ".github" / "workflows" / "ci.yaml"
    assert workflow_path.is_file(), "hosted CI workflow is missing"
    raw = workflow_path.read_text()
    workflow = yaml.safe_load(raw)
    # Least privilege top-level permissions, no write scopes, no pull_request_target.
    assert workflow["permissions"] == {"contents": "read"}
    assert "pull_request_target" not in raw
    for trigger in ("push", "pull_request"):
        assert trigger in workflow[True] or trigger in workflow.get("on", {})
    # Every third-party action is pinned to a full commit SHA.
    uses = re.findall(r"uses:\s*(\S+)", raw)
    assert uses, "no actions used"
    for reference in uses:
        assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), reference
    assert "# v" in raw, "pinned actions should carry a human-readable version comment"
    # The canonical local gate must be executed rather than re-implemented weaker.
    assert "scripts/ci.sh" in raw
    # Container work must be present: build, compose validation, smoke.
    assert "scripts/container_smoke.sh" in raw
    smoke = (ROOT / "scripts" / "container_smoke.sh").read_text()
    assert "docker compose build" in smoke
    assert "docker compose config" in smoke
    assert "docker compose up" in smoke
    assert "docker compose down" in smoke
    # No publication, registry access or deployment command.
    lowered = raw.lower()
    for forbidden in (
        "docker push",
        "docker login",
        "gh release",
        "kubectl",
        "helm upgrade",
        "secrets.",
        "pull_request_target",
        "write-all",
    ):
        assert forbidden not in lowered, forbidden
    # Artifacts are uploaded for diagnosis (test log, audit log, coverage data).
    assert "upload-artifact" in raw
    assert ".coverage" in raw and "pip-audit.log" in raw
    # Runtime deps come from the hash lock; dev tools are pinned by version and
    # installed afterward so they cannot move the runtime surface.
    assert "--require-hashes -r requirements.lock" in raw
    assert "python -m pip check" in raw
    # Compose mounts secrets as bind mounts, so the container user must own them.
    assert 'chown -R "$UID_TARGET:$GID_TARGET" state secrets' in smoke
    assert "chmod 400 secrets/*" in smoke
    deployment_doc = (ROOT / "docs" / "DEPLOYMENT.md").read_text()
    assert "sudo chown -R 10001:10001 secrets" in deployment_doc
    assert "secret file unreadable" in deployment_doc

    # The smoke script exercises the schema gate in both directions: preflight
    # must refuse an unmigrated database and pass after the migration service.
    assert "preflight passed on an unmigrated database" in smoke
    assert smoke.index("refuse to pass before migrations") < smoke.index(
        "docker compose run --rm migrate"
    )
    assert smoke.index("preflight after migration") > smoke.index(
        "docker compose run --rm migrate"
    )

    # The container job runs deployment assertions that import the application,
    # so it must install the runtime dependencies before pytest runs (otherwise
    # pytest exits 4 and the job is red for an environment reason).
    container = yaml.safe_load(raw)["jobs"]["container"]
    runs = [
        step.get("run", "") for step in container["steps"] if isinstance(step, dict)
    ]
    deployment_run = next(run for run in runs if "tests/test_deployment.py" in run)
    lines = [line.strip() for line in deployment_run.splitlines()]
    pytest_line = next(
        index for index, line in enumerate(lines) if line.startswith("python3 -m pytest")
    )
    assert any("requirements.lock" in line for line in lines[:pytest_line]), lines
    assert any(line.startswith("python3 -m pip install --upgrade pip") for line in lines)

    # Anything piped into `tee` must set `pipefail`, otherwise tee's exit status
    # masks a failing gate and the job reports success for a broken build.
    for step in workflow["jobs"]["quality"]["steps"] + workflow["jobs"]["container"]["steps"]:
        run = step.get("run", "") if isinstance(step, dict) else ""
        if "| tee " in run:
            assert "pipefail" in run, run

    # The gate measures coverage and the workflow audits the lockfile.
    assert 'TG_COVERAGE: "1"' in raw
    assert "pip-audit -r requirements.lock --strict" in raw


def test_release_build_script_refuses_to_publish_and_records_provenance(tmp_path):
    import subprocess
    import sys

    output = tmp_path / "out"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/build_release.py"),
            "--output-dir",
            str(output),
            "--dry-run",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    provenance = json.loads(result.stdout)
    assert provenance["version"]
    assert provenance["schema_revision"]
    assert provenance["source_revision"]
    assert provenance["publish"] is False
    assert provenance["dirty"] in (True, False)
    assert not output.exists() or not any(output.iterdir())
    with pytest.raises(subprocess.CalledProcessError):
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build_release.py"),
                "--output-dir",
                str(tmp_path / "publish"),
                "--publish",
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )


def test_release_build_is_byte_reproducible(tmp_path):
    """Two builds of the same source must produce an identical wheel.

    The script pins SOURCE_DATE_EPOCH to the source commit time; without it the
    wheel embeds wall-clock timestamps and the artifact cannot be compared.
    """
    import subprocess
    import sys

    digests = []
    for name in ("first", "second"):
        output = tmp_path / name
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build_release.py"),
                "--output-dir",
                str(output),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        provenance = json.loads((output / "release-provenance.json").read_text())
        assert provenance["source_date_epoch"] > 0
        (wheel,) = output.glob("*.whl")
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        digests.append(digest)
        assert provenance["artifacts"][wheel.name] == digest
    assert digests[0] == digests[1], "release build is not reproducible"


def test_dockerfile_installs_the_pinned_build_backends_before_building():
    """An image build regression found only on a real runner (hosted CI, 2026-11-07).

    The project is installed with `--no-build-isolation`, so `setuptools` and
    `wheel` must already exist in the builder stage. `python:3.11-slim` has no
    `wheel`, so the build failed with exit code 2 before this was pinned.
    """
    dockerfile = (ROOT / "Dockerfile").read_text()
    build_index = dockerfile.index("--no-build-isolation")
    backend_index = dockerfile.index('"setuptools==')
    assert backend_index < build_index
    build_system = (ROOT / "pyproject.toml").read_text().split("build-backend")[0]
    for requirement in re.findall(r'"((?:setuptools|wheel)==[^"]+)"', build_system):
        assert f'"{requirement}"' in dockerfile, requirement
