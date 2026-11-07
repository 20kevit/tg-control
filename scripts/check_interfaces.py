"""CI gate for interface contracts (HTTP / MCP / CLI / Gateway Core).

Usage: python scripts/check_interfaces.py [--write]

Checks performed (each fails closed with a non-zero exit code):
1. The committed matrix `docs/contracts/interfaces.json` matches the registry.
2. Every registered MCP operation is present in the live MCP tool catalog.
3. Every registered CLI operation resolves to a real CLI subcommand.
4. Owner-plane operations are never exposed as MCP tools.
5. No operation is registered more than once per interface binding.

`--write` regenerates the committed matrix instead of checking it. This script is
the single entry point referenced by `docs/TESTING.md`; it is a strict subset of
`tests/test_contracts.py`, which additionally boots the app to compare live routes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "contracts" / "interfaces.json"
sys.path.insert(0, str(ROOT / "src"))

from tg_control import contracts  # noqa: E402


def mcp_tool_names() -> set[str]:
    from tg_control import mcp_server

    return {tool.name for tool in mcp_server.TOOLS}


def cli_subcommands() -> set[str]:
    from tg_control.cli import build_parser

    parser = build_parser()
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return set(action.choices)
    return set()


def check() -> list[str]:
    problems: list[str] = []
    report = contracts.compatibility_report()

    tools = mcp_tool_names()
    commands = cli_subcommands()
    seen_mcp: dict[str, str] = {}
    seen_cli: dict[str, str] = {}
    for operation in report["operations"]:
        name = operation["name"]
        tool = operation["mcp_tool"]
        if tool:
            if tool in seen_mcp:
                problems.append(f"duplicate MCP tool binding: {tool} ({seen_mcp[tool]}, {name})")
            seen_mcp[tool] = name
            if tool not in tools:
                problems.append(f"MCP tool missing from catalog: {tool} ({name})")
        command = operation["cli"]
        if command:
            if command in seen_cli:
                problems.append(f"duplicate CLI binding: {command} ({seen_cli[command]}, {name})")
            seen_cli[command] = name
            if command not in commands:
                problems.append(f"CLI command missing: {command} ({name})")
        if operation["principal"] == "owner" and tool:
            problems.append(f"owner-plane operation exposed over MCP: {name}")

    committed = json.loads(MATRIX.read_text())
    if committed != report:
        problems.append("docs/contracts/interfaces.json is stale; run --write")
    return problems


def main(argv: list[str]) -> int:
    if argv[1:] == ["--write"]:
        MATRIX.parent.mkdir(parents=True, exist_ok=True)
        MATRIX.write_text(json.dumps(contracts.compatibility_report(), indent=2) + "\n")
        print(f"wrote {MATRIX.relative_to(ROOT)}")
        return 0
    if argv[1:]:
        print("usage: check_interfaces.py [--write]", file=sys.stderr)
        return 2
    problems = check()
    report = contracts.compatibility_report()
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    print(
        "interface contracts OK: "
        f"{len(report['operations'])} operations, "
        f"{len(mcp_tool_names())} MCP tools, "
        f"{len(cli_subcommands())} CLI commands"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
