"""CLI is an HTTP client; no Telegram adapter, policy logic or local bypass.

Every command is a transport binding of the same operation the HTTP API and MCP
expose (see `contracts.py`). The CLI never talks to the database or an adapter,
and it never decides authorization: it prints the gateway's normalized result and
exits non-zero for a failed or ambiguous outcome.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from .config import _read_secret
from .http_client import GatewayHttpClient, valid_batch_items

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# command -> (method, path template, whether a JSON body is required)
CLIENT_COMMANDS: dict[str, tuple[str, str, bool]] = {
    "accounts": ("GET", "/v1/accounts", False),
    "capabilities": ("GET", "/v1/accounts/{account_id}/capabilities", False),
    "execute": ("POST", "/v1/execute", True),
    "jobs": ("GET", "/v1/jobs", False),
    "jobs-create": ("POST", "/v1/jobs", True),
    "jobs-get": ("GET", "/v1/jobs/{job_id}", False),
    "jobs-pause": ("POST", "/v1/jobs/{job_id}/pause", False),
    "jobs-resume": ("POST", "/v1/jobs/{job_id}/resume", False),
    "jobs-cancel": ("POST", "/v1/jobs/{job_id}/cancel", False),
    "commands": ("GET", "/v1/commands", False),
    "save": ("POST", "/v1/commands/save", True),
    "events": ("GET", "/v1/events/subscriptions", False),
    "events-subscribe": ("POST", "/v1/events/subscriptions", True),
    "events-unsubscribe": ("DELETE", "/v1/events/subscriptions/{subscription_id}", False),
    "events-pause": ("POST", "/v1/events/subscriptions/{subscription_id}/pause", False),
    "events-resume": ("POST", "/v1/events/subscriptions/{subscription_id}/resume", False),
    "events-pull": ("POST", "/v1/events/subscriptions/{subscription_id}/pull", False),
    "events-ack": ("POST", "/v1/events/subscriptions/{subscription_id}/ack", False),
    "events-nack": ("POST", "/v1/events/subscriptions/{subscription_id}/nack", False),
    "ai-tasks": ("GET", "/v1/ai/tasks", False),
    "ai-run": ("POST", "/v1/ai/tasks/{task}/run", True),
    "health": ("GET", "/health/ready", False),
    "health-dependencies": ("GET", "/health/dependencies", False),
    "diagnose": ("GET", "/v1/admin/diagnostics", False),
    "metrics": ("GET", "/v1/admin/metrics", False),
    "audit": ("GET", "/v1/admin/audit", False),
}
ID_COMMANDS = {
    "jobs-get",
    "jobs-pause",
    "jobs-resume",
    "jobs-cancel",
    "events-unsubscribe",
    "events-pause",
    "events-resume",
    "events-pull",
    "events-ack",
    "events-nack",
}


def _add_common(sub: argparse.ArgumentParser, name: str) -> None:
    if name in {"jobs", "audit"}:
        sub.add_argument("--limit", type=int, default=25, help="page size (1-100)")
        sub.add_argument("--cursor", default=None, help="opaque cursor from a previous page")
    if name in ID_COMMANDS:
        sub.add_argument(
            "--job-id" if name.startswith("jobs") else "--subscription-id",
            required=True,
            help="UUID",
        )
    if name == "capabilities":
        sub.add_argument("--account-id", required=True, help="explicit account UUID")
    if name == "events-pull":
        sub.add_argument("--limit", type=int, default=50, help="events per page (1-100)")
    if name == "events-ack":
        sub.add_argument("--sequence", type=int, required=True)
    if name == "events-nack":
        sub.add_argument("--event-id", required=True)
        sub.add_argument("--reason", default=None)
    if name == "ai-run":
        sub.add_argument("--task", required=True)
        sub.add_argument("--provider", required=True)
        sub.add_argument("--model", default=None)
        sub.add_argument("--file", default="-", help="JSON input file (default stdin)")
    if name in {"execute", "jobs-create", "save", "events-subscribe", "ai-run"}:
        if name != "ai-run":
            sub.add_argument("--file", default="-", help="JSON input file (default stdin)")
    if name == "metrics":
        sub.add_argument("--format", choices=["prometheus", "json"], default="prometheus")


def build_parser() -> argparse.ArgumentParser:
    """Exposed so contract tests can verify CLI/HTTP/MCP parity."""
    parser = argparse.ArgumentParser(prog="tg-control", description="Synthetic gateway API client")
    parser.add_argument("--url", default=os.environ.get("TG_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--token-file",
        default=os.environ.get("TG_TOKEN_FILE"),
        required=not os.environ.get("TG_TOKEN_FILE"),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    for name in CLIENT_COMMANDS:
        _add_common(sub.add_parser(name, help=f"call {CLIENT_COMMANDS[name][1]}"), name)
    request = sub.add_parser("request", help="call a specific /v1/ API route")
    request.add_argument("method", choices=["GET", "POST", "PUT", "PATCH", "DELETE"])
    request.add_argument("path", help="/v1/ path without query string")
    request.add_argument("--file", help="JSON input file, or - for stdin")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    try:
        token = _read_secret(args.token_file)
        client = GatewayHttpClient(args.url, token)
        method, path, body = _build(args)
        status, response = asyncio.run(client.request(method, path, body))
        if isinstance(response, (dict, list)):
            print(json.dumps(response, ensure_ascii=False, indent=2))
        else:  # Prometheus text
            print(response, end="" if str(response).endswith("\n") else "\n")
        if _response_failed(status, response, method=method, path=path, body=body):
            raise SystemExit(1)
    except (ValueError, OSError, KeyError):
        print("Invalid local configuration or input.", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        print("Gateway request failed; a mutation outcome may be unknown.", file=sys.stderr)
        raise SystemExit(1) from None


def _build(args) -> tuple[str, str, dict | None]:
    name = args.command
    if name == "request":
        path = args.path
        if not re.fullmatch(r"/v1/[A-Za-z0-9_/-]+", path) or "//" in path:
            raise ValueError("invalid API path")
        return args.method, path, _json_file(args.file) if args.file else None
    method, template, needs_body = CLIENT_COMMANDS[name]
    path = template
    if "{job_id}" in path:
        path = path.replace("{job_id}", _uuid(args.job_id))
    if "{account_id}" in path:
        path = path.replace("{account_id}", _uuid(args.account_id))
    if "{subscription_id}" in path:
        path = path.replace("{subscription_id}", _uuid(args.subscription_id))
    if "{task}" in path:
        if not re.fullmatch(r"[a-z0-9_.-]{1,64}", args.task):
            raise ValueError("invalid task name")
        path = path.replace("{task}", args.task)
    query: list[str] = []
    if name in {"jobs", "audit"}:
        if not 1 <= args.limit <= 100:
            raise ValueError("limit out of range")
        query.append(f"limit={args.limit}")
        if args.cursor is not None:
            if not isinstance(args.cursor, str) or len(args.cursor) > 512:
                raise ValueError("invalid cursor")
            query.append("cursor=" + _quote(args.cursor))
    if name == "metrics":
        query.append(f"format={args.format}")
    if query:
        path = path + "?" + "&".join(query)
    body = _json_file(args.file) if needs_body else None
    if name == "events-pull":
        body = {"limit": args.limit}
    elif name == "events-ack":
        body = {"sequence": args.sequence}
    elif name == "events-nack":
        body = {"event_id": _uuid(args.event_id)}
        if args.reason:
            body["reason"] = args.reason[:64]
    elif name == "ai-run":
        body = {"input": _json_file(args.file), "provider": args.provider}
        if args.model:
            body["model"] = args.model
    return method, path, body


def _uuid(value: str) -> str:
    if not isinstance(value, str) or not UUID_RE.match(value.lower()):
        raise ValueError("invalid UUID")
    return value.lower()


def _quote(value: str) -> str:
    from urllib.parse import quote

    return quote(value, safe="-_.")


def _response_failed(status: int, response, *, method: str, path: str, body: dict | None) -> bool:
    if not 200 <= status < 300:
        return True
    if isinstance(response, str):
        return False
    if "error" in response:
        return True
    if method != "POST" or not path.startswith("/v1/execute"):
        return False
    actions = body.get("actions") if isinstance(body, dict) else None
    if not isinstance(actions, list) or any(
        not isinstance(action, dict) or type(action.get("operation")) is not str
        for action in actions
    ):
        return True
    operations = [action["operation"] for action in actions]
    if not isinstance(response, dict) or not valid_batch_items(response, operations):
        return True
    return any(item["status"] != "success" for item in response["items"])


def _json_file(filename: str) -> dict:
    if filename == "-":
        content = sys.stdin.read(2_097_153)
    else:
        with Path(filename).open(encoding="utf-8") as stream:
            content = stream.read(2_097_153)
    if len(content.encode("utf-8")) > 2_097_152:
        raise ValueError("input too large")
    value = json.loads(content)
    if not isinstance(value, dict):
        raise ValueError("JSON object required")
    return value


if __name__ == "__main__":
    main()
