"""Host-process lifecycle evidence for the deployment surface.

Runs the real uvicorn process against a disposable PostgreSQL server: readiness
before traffic, synthetic API smoke, SIGTERM graceful shutdown, restart with
persisted state, secret-free logs, and the documented failure modes (missing or
invalid configuration, unreachable database, stale schema, insecure state
directory). Container-level evidence requires a Docker daemon and is recorded in
`docs/DEPLOYMENT.md`; this test never claims to replace it.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_host_process_lifecycle_and_failure_modes():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/runtime_smoke.py"), "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["ok"] is True, report
    scenarios = {item["scenario"]: item for item in report["results"]}
    for required in (
        "health_endpoints",
        "api_smoke_and_state",
        "graceful_sigterm",
        "restart_persistence",
        "log_secret_scan",
        "missing_configuration",
        "invalid_configuration",
        "database_unavailable",
        "schema_mismatch_refused",
        "insecure_state_dir_refused",
    ):
        assert required in scenarios, required
        assert scenarios[required]["ok"] is True, scenarios[required]
    assert scenarios["log_secret_scan"]["evidence"]["leaked"] == []
    assert scenarios["graceful_sigterm"]["evidence"]["forced_kill"] is False
