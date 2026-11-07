"""Regenerate docs/TRACEABILITY.md from the test suite.

Usage: python scripts/gen_traceability.py
The matrix is verified by tests/test_traceability.py after generation.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

AREAS: list[tuple[str, list[str], list[str]]] = [
    ("Legal / privacy gate", ["docs/COMPLIANCE.md", "docs/PRD.md", "docs/SECURITY.md"],
     ["tests/test_operational_config.py"]),
    ("TDLib / accounts (offline boundary)",
     ["src/tg_control/adapters/tdlib_boundary.py", "src/tg_control/adapters/tdlib_poc.py",
      "src/tg_control/adapters/tdlib_read_model.py", "src/tg_control/adapters/tdlib_outcomes.py"],
     ["tests/test_tdlib_boundary.py", "tests/test_adapter.py", "tests/test_tdlib_read_model.py",
      "tests/test_tdlib_outcomes.py", "tests/test_native_tdlib.py"]),
    ("Clients and policy",
     ["src/tg_control/auth.py", "src/tg_control/policy.py", "src/tg_control/core.py"],
     ["tests/test_policy_auth.py", "tests/test_core.py"]),
    ("Read / search / history / media",
     ["src/tg_control/core.py", "src/tg_control/adapters/synthetic.py", "src/tg_control/media.py"],
     ["tests/test_core.py", "tests/test_media.py", "tests/test_retention.py"]),
    ("Write / admin / batch", ["src/tg_control/core.py", "src/tg_control/schemas.py"],
     ["tests/test_adapter.py", "tests/test_core.py"]),
    ("Commands and media pipeline", ["src/tg_control/commands.py", "src/tg_control/media.py"],
     ["tests/test_commands.py", "tests/test_commands_api.py", "tests/test_media.py"]),
    ("Events", ["src/tg_control/events.py"],
     ["tests/test_events.py", "tests/test_events_jobs_api.py"]),
    ("Durable jobs", ["src/tg_control/jobs.py", "src/tg_control/job_kinds.py"],
     ["tests/test_jobs.py", "tests/test_events_jobs_api.py"]),
    ("Workflows", ["src/tg_control/workflows.py"],
     ["tests/test_workflows.py", "tests/test_workflows_api.py"]),
    ("AI task contract", ["src/tg_control/ai.py"],
     ["tests/test_ai.py", "tests/test_ai_api.py"]),
    ("Plugins", ["src/tg_control/plugins.py"],
     ["tests/test_plugins.py", "tests/test_plugins_api.py"]),
    ("Interfaces (HTTP/CLI/MCP/panel)",
     ["src/tg_control/api.py", "src/tg_control/cli.py", "src/tg_control/mcp_server.py",
      "src/tg_control/mcp_stdio.py", "src/tg_control/static"],
     ["tests/test_api.py", "tests/test_cli.py", "tests/test_mcp.py", "tests/test_stdio_process.py",
      "tests/test_browser.py"]),
    ("Audit / retention / instance guard",
     ["src/tg_control/audit.py", "src/tg_control/cache.py", "src/tg_control/instance_guard.py",
      "src/tg_control/security.py", "src/tg_control/request_audit.py"],
     ["tests/test_retention.py", "tests/test_api.py", "tests/test_operational_config.py"]),
    ("Backup / recovery", ["src/tg_control/backup.py", "src/tg_control/backup_probe.py"],
     ["tests/test_backup.py", "tests/test_backup_catalogs.py"]),
    ("Migrations / data model", ["src/tg_control/db.py", "alembic/versions"],
     ["tests/test_migrations.py", "tests/test_postgres_api.py"]),
    ("Packaging / deployment config", ["Dockerfile", "compose.yaml", "pyproject.toml"],
     ["tests/test_operational_config.py"]),
    ("Owner notification / monitoring", ["src/tg_control/monitoring.py"],
     ["tests/test_session_monitor.py", "tests/test_monitor_progress.py", "tests/test_api.py"]),
    ("Delivery / evidence",
     [
         "docs/TRACEABILITY.md",
         "docs/ACCEPTANCE.md",
         "docs/ROADMAP.md",
         "scripts/gen_traceability.py",
     ],
     ["tests/test_traceability.py"]),
]


def main() -> None:
    names = {
        path.relative_to(ROOT).as_posix(): re.findall(
            r"^(?:async )?def (test_[A-Za-z0-9_]+)\(", path.read_text(), re.M
        )
        for path in sorted(TESTS.glob("test_*.py"))
    }
    out = [
        "# Traceability matrix",
        "",
        "This file maps requirement areas to the implementation modules and the exact "
        "executed tests that evidence them. It is verified by `tests/test_traceability.py`, "
        "which fails if a listed file or test function disappears or if a test file is "
        "missing from the matrix. Synthetic evidence proves only synthetic behavior; see "
        "`docs/ACCEPTANCE.md` for what remains open.",
        "",
        "Regenerate with `python scripts/gen_traceability.py` after changing tests.",
        "",
        "| Area | Implementation | Test files | Executed tests |",
        "|---|---|---|---|",
    ]
    appendix = ["", "## Executed test inventory", ""]
    for area, sources, test_files in AREAS:
        existing_sources = [src for src in sources if (ROOT / src).exists()]
        existing_tests = [path for path in test_files if (ROOT / path).exists()]
        if not existing_sources or not existing_tests:
            raise SystemExit(f"area without evidence: {area}")
        count = sum(len(names.get(path, [])) for path in existing_tests)
        out.append(
            "| {} | {} | {} | {} |".format(
                area,
                ", ".join(f"`{src}`" for src in existing_sources),
                ", ".join(f"`{path}`" for path in existing_tests),
                count,
            )
        )
    for path, tests in names.items():
        appendix.append(f"### `{path}`")
        appendix.append("")
        for test in tests:
            appendix.append(f"- `{path}::{test}`")
        appendix.append("")
    (ROOT / "docs" / "TRACEABILITY.md").write_text("\n".join(out + appendix).rstrip() + "\n")


if __name__ == "__main__":
    main()
