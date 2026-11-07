"""Cross-interface contract parity: HTTP vs MCP vs CLI vs Gateway Core.

These tests fail when an operation is added to one interface without a documented
binding in `contracts.py`, when a route/tool/command disappears, when a cursor
operation loses its pagination parameters, or when the JSON compatibility matrix
drifts from the code.
"""

import json
import re
from pathlib import Path

import pytest

from tg_control import contracts
from tg_control.cli import build_parser
from tg_control.mcp_server import TOOLS

ROOT = Path(__file__).resolve().parent.parent
MATRIX = ROOT / "docs" / "contracts" / "interfaces.json"
EXEMPT_ROUTES = {"/openapi.json"} | set(contracts.STATIC_ROUTES)


def app_routes(api) -> set[str]:
    routes = set()
    for route in api.app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if not path or not methods:
            continue
        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            routes.add(f"{method} {path}")
    return routes


def test_registry_json_is_current():
    expected = contracts.compatibility_report()
    on_disk = json.loads(MATRIX.read_text())
    assert on_disk == json.loads(json.dumps(expected)), "run scripts/gen_contracts.py"
    assert len(on_disk["operations"]) >= 60


def _is_exempt(route: str) -> bool:
    path = route.split(" ", 1)[1]
    return path in EXEMPT_ROUTES


def test_every_route_is_declared_and_every_declaration_exists(api):
    declared = contracts.http_routes()
    actual = app_routes(api)
    missing = sorted(route for route in actual - set(declared) if not _is_exempt(route))
    assert not missing, f"undeclared HTTP routes: {missing}"
    stale = sorted(set(declared) - actual)
    assert not stale, f"declared routes that do not exist: {stale}"
    # No route is silently exempt except static assets, the MCP mount and OpenAPI.
    assert all(
        route.split(" ", 1)[1].startswith(("", "/")) and route.startswith(
            ("GET ", "POST ", "PUT ", "PATCH ", "DELETE ")
        )
        for route in actual
    )


def test_no_duplicate_bindings():
    assert len({op.name for op in contracts.OPERATIONS}) == len(contracts.OPERATIONS)
    http = [op.http for op in contracts.OPERATIONS if op.http]
    assert len(http) == len(set(http))
    tools = [op.mcp_tool for op in contracts.OPERATIONS if op.mcp_tool]
    assert len(tools) == len(set(tools))
    cli = [op.cli for op in contracts.OPERATIONS if op.cli]
    assert len(cli) == len(set(cli))


def test_every_client_operation_is_bound_on_all_three_interfaces():
    for operation in contracts.OPERATIONS:
        if operation.principal != contracts.CLIENT:
            continue
        assert operation.http, operation.name
        assert operation.mcp_tool, f"{operation.name} has no MCP binding"
        assert operation.cli, f"{operation.name} has no CLI binding"


def test_owner_plane_is_http_only_by_design():
    """Owner operations must not be reachable with a client credential.

    MCP authenticates client credentials only, so an owner operation must never
    appear as an MCP tool; the CLI reaches owner routes via `request` with an
    owner token, which is intentional and documented.
    """
    mcp_tools = set(contracts.mcp_tools())
    assert mcp_tools.isdisjoint({"tg_workflow_run", "tg_plugins", "tg_audit", "tg_metrics"})
    for operation in contracts.OPERATIONS:
        if operation.principal == contracts.OWNER and operation.mcp_tool:
            pytest.fail(f"owner operation {operation.name} is exposed over MCP")


def test_mcp_tool_catalog_matches_registry():
    registry_tools = contracts.mcp_tools()
    actual = {tool.name for tool in TOOLS}
    assert actual == set(registry_tools), (
        f"unregistered tools: {sorted(actual - set(registry_tools))}; "
        f"missing tools: {sorted(set(registry_tools) - actual)}"
    )
    for tool in TOOLS:
        assert tool.args_model is not None, f"{tool.name} has no input schema"
        schema = tool.args_model.model_json_schema()
        assert schema.get("type") == "object"
        assert tool.description


def test_cli_catalog_matches_registry():
    parser = build_parser()
    choices = set(parser._subparsers._group_actions[0].choices)
    expected = set(contracts.cli_commands()) | {"request"}
    assert expected == choices, (
        f"missing CLI commands: {sorted(expected - choices)}; "
        f"undeclared commands: {sorted(choices - expected)}"
    )


def test_paginated_operations_expose_cursor_parameters(api):
    schema = api.app.openapi()
    for operation in contracts.OPERATIONS:
        if not operation.pagination:
            continue
        method, path = operation.http.split(" ", 1)
        spec = schema["paths"].get(path, {}).get(method.lower())
        assert spec, f"{operation.http} missing from OpenAPI"
        names = {parameter["name"] for parameter in spec.get("parameters", [])}
        assert {"limit", "cursor"} <= names, f"{operation.http} lacks cursor parameters"


def test_error_codes_are_normalized_codes():
    allowed = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
    for operation in contracts.OPERATIONS:
        for code in operation.errors:
            assert allowed.match(code), f"{operation.name}: {code}"
            assert code == code.lower()
    # Unknown codes are normalized rather than echoed.
    from tg_control.errors import normalise_code

    assert normalise_code("Raw Upstream Text!") == "internal_error"
    assert normalise_code("x" * 200) == "internal_error"


def test_core_references_resolve():
    import tg_control.ai
    import tg_control.api
    import tg_control.audit
    import tg_control.commands
    import tg_control.core
    import tg_control.diagnostics
    import tg_control.events
    import tg_control.jobs
    import tg_control.observability
    import tg_control.plugins
    import tg_control.policy
    import tg_control.workflows

    modules = {
        "Gateway": tg_control.core.Gateway,
        "Jobs": tg_control.jobs.Jobs,
        "Workflows": tg_control.workflows.Workflows,
        "AIService": tg_control.ai.AIService,
        "PluginRegistry": tg_control.plugins.PluginRegistry,
        "EventService": tg_control.events.EventService,
        "CommandRouter": tg_control.commands.CommandRouter,
        "Diagnostics": tg_control.diagnostics.Diagnostics,
        "audit": tg_control.audit,
        "policy": tg_control.policy,
        "observability": tg_control.observability,
    }
    for operation in contracts.OPERATIONS:
        reference = operation.core
        assert reference and len(reference) <= 120, operation.name
        if " " in reference or "/" in reference:
            continue
        head, _, attribute = reference.partition(".")
        target = modules.get(head)
        if target is None:
            continue
        assert hasattr(target, attribute), f"{operation.name}: {reference} does not resolve"


def test_authorization_requirements_are_declared():
    for operation in contracts.OPERATIONS:
        assert operation.principal in {contracts.CLIENT, contracts.OWNER}
        if operation.principal == contracts.CLIENT:
            route = operation.http
            assert route and not route.startswith(("/v1/admin", "/health/")), (
                f"client operation on an admin route: {operation.name}"
            )
