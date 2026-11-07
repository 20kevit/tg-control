#!/usr/bin/env python
"""Process-level runtime lifecycle verification for the gateway.

The container runtime is not available in every build environment, so this tool
exercises the same lifecycle the image relies on directly on the host:

    alembic upgrade head -> uvicorn (readiness-gated) -> API smoke ->
    SIGTERM graceful exit -> restart -> persisted state -> shutdown

It also runs negative configurations (missing/invalid configuration, unreachable
database, stale schema, insecure state directory) and scans all captured output
for secret leakage.

This proves host-process lifecycle behavior. It does **not** prove container
build, image-layer, cgroup or Docker network behavior; those are recorded as
BLOCKED in `docs/DEPLOYMENT.md` when no container runtime is available.

Usage:
    python scripts/runtime_smoke.py [--json] [--keep]
Exit code 0 only when every executed scenario behaved as documented.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
RESULTS: list[dict[str, object]] = []


def record(name: str, ok: bool, detail: str = "", evidence: dict | None = None) -> None:
    RESULTS.append({"scenario": name, "ok": ok, "detail": detail, "evidence": evidence or {}})


def secret_file(path: Path, value: str) -> Path:
    path.write_text(value)
    path.chmod(0o600)
    return path


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for_exit(process: subprocess.Popen, timeout: float) -> int | None:
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None


def http(url: str, token: str, timeout: float = 5.0) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def http_json(url: str, token: str, payload: dict, timeout: float = 10.0) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        body = error.read()
        try:
            return error.code, json.loads(body or b"{}")
        except ValueError:
            return error.code, {}


def start_app(env: dict[str, str], port: int, log_path: Path) -> subprocess.Popen:
    log = log_path.open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tg_control.api:app_factory",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--no-access-log",
            "--no-server-header",
            "--no-proxy-headers",
            "--log-level",
            "info",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    process._log_handle = log  # type: ignore[attr-defined]
    return process


def stop_app(process: subprocess.Popen, *, timeout: float = 30.0) -> dict[str, object]:
    """Stop with SIGTERM and report whether a SIGKILL was required.

    uvicorn re-raises the captured signal after a completed graceful shutdown, so
    a status of ``-SIGTERM`` is the *successful* case; the log must still show the
    application shutdown completing. ``forced`` means the grace window expired.
    """
    if process.poll() is not None:
        return {"code": process.returncode, "forced": False}
    os.killpg(os.getpgid(process.pid), signal.SIGTERM)
    code = wait_for_exit(process, timeout)
    forced = False
    if code is None:
        forced = True
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        process.wait(timeout=10)
        code = process.returncode
    handle = getattr(process, "_log_handle", None)
    if handle is not None:
        handle.close()
    return {"code": code, "forced": forced}


def app_env(
    *,
    database_file: Path,
    owner_file: Path,
    key_file: Path,
    state_dir: Path | None,
    origin: str,
    log_level: str = "INFO",
) -> dict[str, str]:
    env = {
        "PATH": os.defpath,
        "HOME": str(ROOT),
        "LANG": "C.UTF-8",
        "TMPDIR": tempfile.gettempdir(),
        "TG_DATABASE_URL_FILE": str(database_file),
        "TG_OWNER_TOKEN_FILE": str(owner_file),
        "TG_ENCRYPTION_KEY_FILE": str(key_file),
        "TG_ALLOWED_HOSTS": "127.0.0.1,localhost",
        "TG_ALLOWED_ORIGINS": origin,
        "TG_LOG_LEVEL": log_level,
    }
    if state_dir is not None:
        env["TG_SYNTHETIC_STATE_DIR"] = str(state_dir)
    return env


def migrate(database_url: str, revision: str = "head") -> None:
    env = {**os.environ, "TG_DATABASE_URL": database_url}
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=180,
    )


def wait_ready(base: str, token: str, process: subprocess.Popen, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        try:
            status, _ = http(f"{base}/health/ready", token, timeout=2)
            if status == 200:
                return True
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(0.25)
    return False


def scenario_positive(workdir: Path, server) -> None:
    token = "synthetic-runtime-owner-token-" + "r" * 28
    encryption_key = Fernet.generate_key()
    password = "synthetic-runtime-db-password"
    database_url = server.get_uri("smoke_db")
    database_file = secret_file(workdir / "database_url", database_url)
    owner_file = secret_file(workdir / "owner_token", token)
    key_file = secret_file(workdir / "encryption_key", encryption_key.decode())
    state_dir = workdir / "state"
    state_dir.mkdir(mode=0o700)
    migrate(database_url)

    port = free_port()
    origin = f"http://127.0.0.1:{port}"
    env = app_env(
        database_file=database_file,
        owner_file=owner_file,
        key_file=key_file,
        state_dir=state_dir,
        origin=origin,
    )

    log1 = workdir / "run1.log"
    process = start_app(env, port, log1)
    base = f"http://127.0.0.1:{port}"
    ready = wait_ready(base, token, process, timeout=60)
    if not ready:
        stop_app(process)
        record("startup_readiness", False, (log1.read_text(errors="replace"))[-2000:])
        return
    live_status, live_body = http(f"{base}/health/live", token)
    deps_status, _ = http(f"{base}/health/dependencies", token)
    unauth_status, _ = http(f"{base}/health/ready", "wrong-token-" + "x" * 40)
    record(
        "health_endpoints",
        live_status == 200 and deps_status == 200 and unauth_status == 401,
        evidence={"live": live_status, "dependencies": deps_status, "unauthorized": unauth_status,
                  "mode": json.loads(live_body).get("mode")},
    )

    status, account = http_json(
        f"{base}/v1/admin/accounts",
        token,
        {"alias": "synthetic runtime account", "phone_number": "+12025550123"},
    )
    status2, client = http_json(
        f"{base}/v1/admin/clients", token, {"name": "synthetic runtime client"}
    )
    policy_status, _ = 0, None
    request = urllib.request.Request(
        f"{base}/v1/admin/clients/{client.get('id')}/accounts/{account.get('id')}/policy",
        data=json.dumps(
            {
                "read": {"enabled": True, "allow_types": ["public_channel"]},
                "write": {"enabled": True, "allow_chats": ["public-channel"]},
                "admin": {"enabled": False},
            }
        ).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="PUT",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        policy_status = response.status

    credential = client.get("credential", "")
    execute_status, batch = http_json(
        f"{base}/v1/execute",
        credential,
        {
            "actions": [
                {
                    "account_id": account.get("id"),
                    "operation": "send",
                    "chat_id": "public-channel",
                    "text": "synthetic runtime smoke",
                }
            ],
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    accounts_status, _ = http(f"{base}/v1/accounts", credential)
    state_written = any(state_dir.rglob("state.enc"))
    record(
        "api_smoke_and_state",
        status == 201
        and status2 == 201
        and policy_status == 200
        and execute_status == 200
        and batch.get("items", [{}])[0].get("status") == "success"
        and accounts_status == 200
        and state_written,
        evidence={
            "account": status,
            "client": status2,
            "policy": policy_status,
            "execute": execute_status,
            "accounts": accounts_status,
            "synthetic_state_written": state_written,
        },
    )

    # Graceful termination on SIGTERM: no SIGKILL, framework shutdown completed.
    started = time.monotonic()
    stop = stop_app(process)
    shutdown_seconds = round(time.monotonic() - started, 3)
    shutdown_log = log1.read_text(errors="replace")
    graceful = (
        stop["forced"] is False
        and stop["code"] in (0, -signal.SIGTERM)
        and "Application shutdown complete" in shutdown_log
        and shutdown_seconds < 30
    )
    record(
        "graceful_sigterm",
        graceful,
        evidence={
            "exit_code": stop["code"],
            "forced_kill": stop["forced"],
            "shutdown_seconds": shutdown_seconds,
            "log_shutdown_complete": "Application shutdown complete" in shutdown_log,
        },
    )

    # Restart against the same database and state directory: data survives.
    log2 = workdir / "run2.log"
    process = start_app(env, port, log2)
    ready = wait_ready(base, token, process, timeout=60)
    persisted_status, persisted_body = http(f"{base}/v1/accounts", credential)
    try:
        persisted = json.loads(persisted_body or b"{}")
    except ValueError:
        persisted = {}
    persisted_ok = (
        ready
        and persisted_status == 200
        and persisted.get("accounts")
        and persisted["accounts"][0]["id"] == account.get("id")
    )
    stop2 = stop_app(process)
    record(
        "restart_persistence",
        persisted_ok and stop2["code"] in (0, -signal.SIGTERM) and not stop2["forced"],
        evidence={
            "ready": ready,
            "accounts": persisted_status,
            "exit_code": stop2["code"],
            "forced_kill": stop2["forced"],
        },
    )

    # No secret material may appear in either run's captured output.
    logs = log1.read_text(errors="replace") + log2.read_text(errors="replace")
    leaked = [
        label
        for label, value in (
            ("owner_token", token),
            ("encryption_key", encryption_key.decode()),
            ("database_url", database_url),
            ("db_password", password),
        )
        if value and value in logs
    ]
    record("log_secret_scan", not leaked, evidence={"leaked": leaked, "log_bytes": len(logs)})


def scenario_missing_config(workdir: Path) -> None:
    port = free_port()
    env = {
        "PATH": os.defpath,
        "HOME": str(ROOT),
        "TG_DATABASE_URL_FILE": str(workdir / "absent_database_url"),
    }
    process = start_app(env, port, workdir / "missing.log")
    code = wait_for_exit(process, 60)
    output = (workdir / "missing.log").read_text(errors="replace")
    stop_app(process)
    record(
        "missing_configuration",
        code not in (0, None) and "missing required configuration" in output,
        evidence={"exit_code": code, "diagnostic": "missing required configuration" in output},
    )


def scenario_invalid_config(workdir: Path) -> None:
    short_token = secret_file(workdir / "short_owner", "too-short")
    key = secret_file(workdir / "bad_key", Fernet.generate_key().decode())
    db = secret_file(workdir / "invalid_database_url", "postgresql+psycopg://u:p@127.0.0.1:1/none")
    port = free_port()
    env = app_env(
        database_file=db,
        owner_file=short_token,
        key_file=key,
        state_dir=None,
        origin=f"http://127.0.0.1:{port}",
    )
    process = start_app(env, port, workdir / "invalid.log")
    code = wait_for_exit(process, 60)
    output = (workdir / "invalid.log").read_text(errors="replace")
    stop_app(process)
    record(
        "invalid_configuration",
        code not in (0, None) and "owner token must contain" in output,
        evidence={"exit_code": code, "diagnostic": "owner token must contain" in output},
    )


def scenario_database_unavailable(workdir: Path, token: str, key: bytes) -> None:
    owner = secret_file(workdir / "owner_unavailable", token)
    key_file = secret_file(workdir / "key_unavailable", key.decode())
    port = free_port()
    unused = free_port()  # closed port: nothing listens here
    db = secret_file(workdir / "db_unavailable", f"postgresql+psycopg://u:p@127.0.0.1:{unused}/none")
    env = app_env(
        database_file=db,
        owner_file=owner,
        key_file=key_file,
        state_dir=None,
        origin=f"http://127.0.0.1:{port}",
    )
    process = start_app(env, port, workdir / "db_down.log")
    code = wait_for_exit(process, 60)
    output = (workdir / "db_down.log").read_text(errors="replace")
    stop_app(process)
    lowered = output.lower()
    record(
        "database_unavailable",
        code not in (0, None) and ("could not be inspected" in lowered or "connect" in lowered),
        evidence={"exit_code": code, "mentions_connectivity": "connect" in lowered},
    )


def scenario_schema_mismatch(workdir: Path, server, token: str, key: bytes) -> None:
    database_url = server.get_uri("stale_db")
    migrate(database_url, revision="0006")
    db = secret_file(workdir / "stale_db_url", database_url)
    owner = secret_file(workdir / "owner_stale", token)
    key_file = secret_file(workdir / "key_stale", key.decode())
    port = free_port()
    env = app_env(
        database_file=db,
        owner_file=owner,
        key_file=key_file,
        state_dir=None,
        origin=f"http://127.0.0.1:{port}",
    )
    process = start_app(env, port, workdir / "stale.log")
    code = wait_for_exit(process, 60)
    output = (workdir / "stale.log").read_text(errors="replace")
    stop_app(process)
    record(
        "schema_mismatch_refused",
        code not in (0, None) and "does not match this build" in output,
        evidence={"exit_code": code, "diagnostic": "does not match this build" in output},
    )


def scenario_insecure_state_dir(workdir: Path, server, token: str, key: bytes) -> None:
    database_url = server.get_uri("smoke_db")
    db = secret_file(workdir / "db_insecure", database_url)
    owner = secret_file(workdir / "owner_insecure", token)
    key_file = secret_file(workdir / "key_insecure", key.decode())
    state_dir = workdir / "insecure-state"
    state_dir.mkdir(mode=0o755)
    state_dir.chmod(0o755)
    port = free_port()
    env = app_env(
        database_file=db,
        owner_file=owner,
        key_file=key_file,
        state_dir=state_dir,
        origin=f"http://127.0.0.1:{port}",
    )
    process = start_app(env, port, workdir / "insecure_state.log")
    code = wait_for_exit(process, 60)
    output = (workdir / "insecure_state.log").read_text(errors="replace")
    stop_app(process)
    refused = "state directory must not be group/world accessible" in output.lower()
    record(
        "insecure_state_dir_refused",
        code not in (0, None) and refused,
        evidence={"exit_code": code, "diagnostic": refused},
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit a JSON report on stdout")
    parser.add_argument("--keep", action="store_true", help="keep the disposable work directory")
    parser.add_argument(
        "--only", default="", help="comma-separated selector: all, positive, negative"
    )
    args = parser.parse_args(argv)
    selector = {part.strip() for part in args.only.split(",") if part.strip()} or {"all"}

    workdir = Path(tempfile.mkdtemp(prefix="tg-control-runtime-"))
    server = None
    exit_code = 0
    try:
        import pgserver
        import psycopg

        server = pgserver.get_server(workdir / "pgdata", cleanup_mode="delete")
        with psycopg.connect(server.get_uri(), autocommit=True) as db:
            db.execute("CREATE DATABASE smoke_db")
            db.execute("CREATE DATABASE stale_db")

        token = "synthetic-runtime-owner-token-" + "r" * 28
        key = Fernet.generate_key()

        if selector & {"all", "positive"}:
            scenario_positive(workdir, server)
        if selector & {"all", "negative"}:
            scenario_missing_config(workdir)
            scenario_invalid_config(workdir)
            scenario_database_unavailable(workdir, token, key)
            scenario_schema_mismatch(workdir, server, token, key)
            scenario_insecure_state_dir(workdir, server, token, key)
    except Exception as exc:  # pragma: no cover - harness failure, not app failure
        record("harness", False, detail=f"{type(exc).__name__}: {exc}")
    finally:
        if server is not None:
            try:
                server.cleanup()
            except Exception:
                pass

    failed = [item["scenario"] for item in RESULTS if not item["ok"]]
    exit_code = 1 if failed else 0
    report = {
        "ok": not failed,
        "failed": failed,
        "results": RESULTS,
        "workdir": str(workdir) if args.keep else None,
        "note": (
            "Host-process lifecycle only. Container build/runtime behavior requires a Docker "
            "daemon and is recorded as BLOCKED when unavailable."
        ),
    }
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        for item in RESULTS:
            print(f"[{'PASS' if item['ok'] else 'FAIL'}] {item['scenario']} {item['evidence']}")
        print("runtime smoke:", "ok" if not failed else f"failed: {failed}")
    if not args.keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
