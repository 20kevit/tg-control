"""Regenerate docs/TRACEABILITY.md from the test suite.

Usage: python scripts/gen_traceability.py
The matrix is verified by tests/test_traceability.py after generation.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# Requirement -> contract mapping and the exact evidence level for each area.
# Allowed labels:
#   Implemented     code exists, no executed test for this area yet
#   Locally tested  executed by the local gate in this environment
#   Container-tested   executed against a built image under a real runtime
#   Hosted-CI-tested   executed by the hosted workflow recorded in STATUS.md
#   Not tested      no execution attempted
#   Blocked         cannot execute here; limitation recorded in STATUS.md
#   Requires owner action  needs approval (release, deployment, credentials)
LEVELS: dict[str, tuple[str, str]] = {
    "Legal / privacy gate": ("COMPLIANCE.md, PRD.md", "Blocked"),
    "TDLib / accounts (offline boundary)": ("TDLIB_OFFLINE.md", "Locally tested"),
    "Clients and policy": ("SECURITY.md, POLICY.md", "Locally tested"),
    "Read / search / history / media": ("API.md", "Locally tested"),
    "Write / admin / batch": ("API.md", "Locally tested"),
    "Commands and media pipeline": ("COMMANDS_MEDIA_AI_PLUGINS.md", "Locally tested"),
    "Events": ("EVENTS_JOBS_WORKFLOWS.md", "Locally tested"),
    "Durable jobs": ("EVENTS_JOBS_WORKFLOWS.md", "Locally tested"),
    "Workflows": ("EVENTS_JOBS_WORKFLOWS.md", "Locally tested"),
    "AI task contract": ("COMMANDS_MEDIA_AI_PLUGINS.md", "Locally tested"),
    "Plugins": ("COMMANDS_MEDIA_AI_PLUGINS.md", "Locally tested"),
    "Interfaces (HTTP/CLI/MCP/panel)": ("API.md, MCP.md, CLI.md", "Locally tested"),
    "Audit / retention / instance guard": ("SECURITY.md, OPERATIONS.md", "Locally tested"),
    "Backup / recovery": ("BACKUP_RESTORE.md, OPERATIONS.md", "Locally tested"),
    "Interface contracts (parity matrix)": ("CONTRACT_INDEX.md", "Locally tested"),
    "Cursor pagination": ("API.md", "Locally tested"),
    "Observability / health / diagnostics": ("OPERATIONS.md", "Locally tested"),
    "Authentication throttling": ("SECURITY.md", "Locally tested"),
    "Security property boundaries": ("SECURITY.md", "Locally tested"),
    "Migrations / data model": ("DATA_MODEL.md", "Locally tested"),
    "Packaging / deployment config": ("DEPLOYMENT.md, RELEASE_PROCESS.md", "Locally tested"),
    "Owner notification / monitoring": ("OPERATIONS.md", "Locally tested"),
    "Deployment lifecycle (host process)": ("DEPLOYMENT.md", "Locally tested"),
    "Release metadata and provenance": ("RELEASE_PROCESS.md", "Locally tested"),
    "Hosted CI pipeline": ("RELEASE_PROCESS.md", "Not tested"),
    "Delivery / evidence": ("ACCEPTANCE.md, TRACEABILITY.md", "Locally tested"),
}

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
     ["tests/test_retention.py", "tests/test_retention_pruning.py", "tests/test_api.py",
      "tests/test_operational_config.py"]),
    ("Backup / recovery", ["src/tg_control/backup.py", "src/tg_control/backup_probe.py"],
     ["tests/test_backup.py", "tests/test_backup_catalogs.py",
      "tests/test_disaster_recovery.py"]),
    (
        "Interface contracts (parity matrix)",
        [
            "src/tg_control/contracts.py",
            "docs/contracts/interfaces.json",
            "scripts/gen_contracts.py",
        ],
        ["tests/test_contracts.py"],
    ),
    ("Cursor pagination", ["src/tg_control/pagination.py"], ["tests/test_pagination.py"]),
    (
        "Observability / health / diagnostics",
        ["src/tg_control/observability.py", "src/tg_control/diagnostics.py"],
        ["tests/test_observability.py"],
    ),
    ("Authentication throttling", ["src/tg_control/throttle.py"], ["tests/test_throttle.py"]),
    (
        "Security property boundaries",
        [
            "src/tg_control/errors.py",
            "src/tg_control/pagination.py",
            "src/tg_control/observability.py",
        ],
        ["tests/test_property_boundaries.py"],
    ),
    ("Migrations / data model", ["src/tg_control/db.py", "alembic/versions"],
     ["tests/test_migrations.py", "tests/test_postgres_api.py"]),
    ("Packaging / deployment config",
     ["Dockerfile", ".dockerignore", "compose.yaml", ".env.example", "pyproject.toml",
      "requirements.lock"],
     ["tests/test_operational_config.py", "tests/test_deployment.py"]),
    ("Deployment lifecycle (host process)",
     ["scripts/runtime_smoke.py", "src/tg_control/config.py", "src/tg_control/healthcheck.py",
      "docs/DEPLOYMENT.md"],
     ["tests/test_runtime_lifecycle.py"]),
    ("Release metadata and provenance",
     ["src/tg_control/version.py", "scripts/build_release.py", "docs/RELEASE_PROCESS.md"],
     ["tests/test_deployment.py"]),
    ("Hosted CI pipeline", [".github/workflows/ci.yaml", "scripts/ci.sh",
                            "scripts/container_smoke.sh"],
     ["tests/test_deployment.py"]),
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
        "Evidence labels: **Implemented** (code only), **Locally tested** (executed by "
        "the local gate), **Container-tested** (built image under a real runtime), "
        "**Hosted-CI-tested** (executed by the hosted workflow), **Not tested**, "
        "**Blocked** (cannot execute here; limitation recorded in STATUS.md), "
        "**Requires owner action**. Only labels backed by recorded execution appear "
        "below: no row is Container-tested or Hosted-CI-tested today.",
        "",
        "| Area | Contract | Implementation | Test files | Executed tests | Evidence |",
        "|---|---|---|---|---|---|",
    ]
    appendix = ["", "## Executed test inventory", ""]
    for area, sources, test_files in AREAS:
        existing_sources = [src for src in sources if (ROOT / src).exists()]
        existing_tests = [path for path in test_files if (ROOT / path).exists()]
        if not existing_sources or not existing_tests:
            raise SystemExit(f"area without evidence: {area}")
        count = sum(len(names.get(path, [])) for path in existing_tests)
        contract, level = LEVELS[area]
        out.append(
            "| {} | {} | {} | {} | {} | {} |".format(
                area,
                contract,
                ", ".join(f"`{src}`" for src in existing_sources),
                ", ".join(f"`{path}`" for path in existing_tests),
                count,
                level,
            )
        )
    out += [
        "",
        "## Executed but not yet evidenced in this environment",
        "",
        "| Requirement | Contract | Status | Why |",
        "|---|---|---|---|",
        "| Container image build, in-container non-root check, "
        "container start/recreate/persistence, database-outage readiness | "
        "DEPLOYMENT.md | Blocked | No Docker/Podman/Buildah binary, no container "
        "socket and `CapEff: 0` in the development sandbox. "
        "`scripts/container_smoke.sh` is ready but unexecuted. |",
        "| Hosted CI run of `.github/workflows/ci.yaml` | RELEASE_PROCESS.md | "
        "Not tested | The workflow is committed and statically asserted, but no "
        "GitHub Actions run has occurred. |",
        "| Image/OS vulnerability scan and base-image digest pinning | "
        "RELEASE_PROCESS.md | Requires owner action | Needs registry/network "
        "access and approval. |",
        "| Publishing an image, GitHub release, or any deployment | "
        "RELEASE_PROCESS.md | Requires owner action | Explicitly out of scope "
        "without owner approval. |",
        "| Native TDLib adapter / live Telegram behavior | COMPLIANCE.md | Blocked | "
        "Compliance and owner gates unresolved. |",
    ]
    for path, tests in names.items():
        appendix.append(f"### `{path}`")
        appendix.append("")
        for test in tests:
            appendix.append(f"- `{path}::{test}`")
        appendix.append("")
    (ROOT / "docs" / "TRACEABILITY.md").write_text("\n".join(out + appendix).rstrip() + "\n")


if __name__ == "__main__":
    main()
