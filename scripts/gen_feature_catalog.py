"""Generate the complete synthetic capability and feature traceability inventories.

Run with: .venv/bin/python scripts/gen_feature_catalog.py
No adapter is started; eligibility remains unknown and no live account is accessed.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from tg_control.capabilities import capability_catalog  # noqa: E402
from tg_control.features import FEATURE_SPECS, SYNTHETIC_FEATURE_OPERATIONS  # noqa: E402

TEST_ROOT = ROOT / "tests"
INVENTORY_TEST = (
    "tests/test_features.py::test_feature_inventory_has_complete_registered_"
    "schemas_and_capabilities"
)
HANDLER_TEST = (
    "tests/test_features.py::test_synthetic_allowlist_has_a_handler_branch_for_every_operation"
)


def _test_references() -> dict[str, list[str]]:
    references: dict[str, list[str]] = {operation: [] for operation in FEATURE_SPECS}
    for path in sorted(TEST_ROOT.glob("test_*.py")):
        try:
            tree = ast.parse(path.read_text())
        except SyntaxError as exc:  # pragma: no cover - malformed tests fail separately
            raise SystemExit(f"cannot parse {path}: {exc}") from exc
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            string_constants = {
                value.value
                for value in ast.walk(node)
                if isinstance(value, ast.Constant) and isinstance(value.value, str)
            }
            for operation in string_constants & references.keys():
                references[operation].append(f"{path.relative_to(ROOT).as_posix()}::{node.name}")
    for operation, tests in references.items():
        tests.append(INVENTORY_TEST)
        if operation in SYNTHETIC_FEATURE_OPERATIONS:
            tests.append(HANDLER_TEST)
        references[operation] = list(dict.fromkeys(tests))
    return references


def _blocker(row: dict) -> str:
    blockers: list[str] = []
    reason = row.get("blocker_reason")
    if reason:
        blockers.append(reason)
    blockers.extend(
        f"{condition}={value}" for condition, value in row.get("conditional_blockers", {}).items()
    )
    if row["operation"] == "translate_message":
        blockers.append("compliance_blocked")
    if not row["adapter_support"].startswith("synthetic_fixture"):
        blockers.append("synthetic_handler_unimplemented")
    if row.get("platform_dependency") and row.get("account_eligible") is not True:
        blockers.append(
            f"{row['platform_dependency']} eligibility "
            f"{'false' if row.get('account_eligible') is False else 'unknown'}"
        )
    return "; ".join(dict.fromkeys(blockers)) or "—"


def _permission_label(row: dict) -> str:
    required = [row["permission"], *row.get("additional_permissions", [])]
    return " + ".join(dict.fromkeys(required))


def _eligibility_label(row: dict) -> str:
    dependency = row.get("platform_dependency")
    if not dependency:
        return "—"
    eligible = row.get("account_eligible")
    state = {True: "eligible", False: "ineligible", None: "unknown"}[eligible]
    return f"{dependency}: {state}"


def _feature_result(operation: str, spec, row: dict) -> str:
    if row["status"] == "compliance_blocked":
        return (
            "No synthetic handler; Core refuses with `compliance_blocked` until "
            "compliance approval."
        )
    if row["status"] == "approval_required":
        return (
            "No synthetic mutation handler; Core refuses with `approval_required`: "
            f"{spec.blocked_reason}"
        )
    if operation in SYNTHETIC_FEATURE_OPERATIONS:
        conditional = ", ".join(
            f"`{when}` → `{error}`" for when, error in spec.conditional_blockers
        )
        suffix = f" Conditional refusal: {conditional}." if conditional else ""
        return (
            f"Synthetic fixture branch in `SyntheticAdapter._execute_feature`; never live.{suffix}"
        )
    if spec.platform_dependency:
        return (
            "No synthetic handler; fails with `capability_unavailable` when the "
            "eligibility flag is false, otherwise `unsupported_platform` "
            f"(dependency: `{spec.platform_dependency}`)."
        )
    return "No synthetic handler; fails closed with `unsupported_operation`."


def _markdown_cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def main() -> None:
    catalog = capability_catalog("catalogue-reference")
    catalog.pop("account_id", None)
    rows = catalog["capabilities"]
    feature_rows = {row["operation"]: row for row in rows if row["operation"] in FEATURE_SPECS}
    test_refs = _test_references()

    if len(FEATURE_SPECS) != 135 or len(SYNTHETIC_FEATURE_OPERATIONS) != 124:
        raise SystemExit(
            "feature inventory counts changed; review this generator before regenerating"
        )
    if len(rows) != 147 or len(feature_rows) != 135:
        raise SystemExit(
            "capability inventory counts changed; review this generator before regenerating"
        )

    published_rows: list[dict] = []
    for row in rows:
        published = dict(row)
        published["synthetic_handler_registered"] = row["adapter_support"] == "synthetic_fixture"
        if row["operation"] in FEATURE_SPECS:
            spec = FEATURE_SPECS[row["operation"]]
            published["requirement"] = spec.description
            published["handler"] = (
                "SyntheticAdapter._execute_feature"
                if row["operation"] in SYNTHETIC_FEATURE_OPERATIONS
                else None
            )
            published["failure_behavior"] = _feature_result(row["operation"], spec, row)
            published["test_references"] = test_refs[row["operation"]]
        published_rows.append(published)

    inventory = {
        "catalogue_context": (
            "Full owner/reference catalogue. Platform eligibility is unknown in this offline "
            "snapshot; each client response is filtered by its current permission and "
            "accessible chats."
        ),
        "counts": {
            "base_operations": 12,
            "feature_actions": 135,
            "synthetic_feature_handlers": 124,
            "feature_actions_without_synthetic_handlers": 11,
            "total_capability_entries": 147,
        },
        **{key: value for key, value in catalog.items() if key != "capabilities"},
        "capabilities": published_rows,
    }
    contracts = ROOT / "docs" / "contracts"
    contracts.mkdir(parents=True, exist_ok=True)
    (contracts / "capability_catalog.json").write_text(
        json.dumps(inventory, indent=2, sort_keys=False) + "\n"
    )

    unsupported = [
        operation for operation in FEATURE_SPECS if operation not in SYNTHETIC_FEATURE_OPERATIONS
    ]
    traceability = [
        "# Feature action traceability",
        "",
        (
            "Generated by `scripts/gen_feature_catalog.py` from `FEATURE_SPECS`, the "
            "synthetic handler allowlist, the full capability catalog, and explicit test "
            "references. This matrix is requirement → action → handler/fail-closed behavior "
            "→ test definition; a static mapping/registration test is not claimed as "
            "end-to-end execution of every action. The machine-readable [147-entry "
            "catalogue](contracts/capability_catalog.json) carries every row's support, "
            "eligibility, blocker, retry, permission, and live-status metadata."
        ),
        "",
        "## The 11 actions without synthetic handlers",
        "",
        (
            "Eligibility is `unknown` in this offline inventory. A future client response "
            "may classify an account-specific dependency differently, but cannot bypass "
            "the implementation or approval blocker."
        ),
        "",
        (
            "| Action | Group / permission | Dependency / eligibility | Catalogue status | "
            "Exact handler outcome / reason | Test evidence |"
        ),
        "|---|---|---|---|---|---|",
    ]
    for operation in unsupported:
        spec = FEATURE_SPECS[operation]
        row = feature_rows[operation]
        traceability.append(
            "| {} | {} / {} | {} | `{}` | {} | {} |".format(
                operation,
                spec.group,
                spec.permission.value,
                _eligibility_label(row),
                row["status"],
                _markdown_cell(_feature_result(operation, spec, row)),
                "; ".join(f"`{ref}`" for ref in test_refs[operation]),
            )
        )

    traceability += [
        "",
        "## All 135 feature requirements mapped to actions, handlers, and tests",
        "",
        (
            "Status in this table is the generated offline snapshot (unknown account "
            "eligibility). `chat_required` and additional permissions are part of the "
            "catalogue metadata. Tests linked by exact operation-name references are "
            "listed alongside the completeness and handler-registration tests; consult "
            "each test for its precise assertion scope."
        ),
        "",
        (
            "| Requirement / action intent | Action | Group | Required permission(s) | "
            "Chat required | Synthetic support | Eligibility | Snapshot status / blocker | "
            "Handler or fail-closed result | Test references |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for operation, spec in FEATURE_SPECS.items():
        row = feature_rows[operation]
        tests = "; ".join(f"`{ref}`" for ref in test_refs[operation])
        handler_status = (
            "registered" if operation in SYNTHETIC_FEATURE_OPERATIONS else "not registered"
        )
        support = f"{handler_status}; {row['adapter_support']}"
        status_blocker = row["status"]
        blocker = _blocker(row)
        if blocker != "—":
            status_blocker += f"; {blocker}"
        traceability.append(
            "| {} | `{}` | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                _markdown_cell(spec.description),
                operation,
                spec.group,
                _permission_label(row),
                "yes" if spec.chat_required else "no",
                support,
                _eligibility_label(row),
                _markdown_cell(status_blocker),
                _markdown_cell(_feature_result(operation, spec, row)),
                tests,
            )
        )

    (ROOT / "docs" / "FEATURE_TRACEABILITY.md").write_text("\n".join(traceability) + "\n")
    print(
        "wrote docs/contracts/capability_catalog.json "
        "(147 entries) and docs/FEATURE_TRACEABILITY.md (135 actions)"
    )


if __name__ == "__main__":
    main()
