"""CLI is an HTTP client; no Telegram adapter or local policy bypass."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path

from .config import _read_secret
from .http_client import GatewayHttpClient


def main() -> None:
    parser = argparse.ArgumentParser(prog="tg-control", description="Synthetic gateway API client")
    parser.add_argument("--url", default=os.environ.get("TG_API_URL", "http://127.0.0.1:8000"))
    parser.add_argument(
        "--token-file",
        default=os.environ.get("TG_TOKEN_FILE"),
        required=not os.environ.get("TG_TOKEN_FILE"),
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("accounts", help="list accounts assigned to this client")
    execute = sub.add_parser("execute", help="execute a batch JSON document from a file or stdin")
    execute.add_argument("--file", default="-", help="JSON input file (default stdin)")
    request = sub.add_parser("request", help="call a specific /v1/ API route")
    request.add_argument("method", choices=["GET", "POST", "PUT", "PATCH"])
    request.add_argument("path", help="/v1/ path without query string")
    request.add_argument("--file", help="JSON input file, or - for stdin")
    args = parser.parse_args()
    try:
        token = _read_secret(args.token_file)
        client = GatewayHttpClient(args.url, token)
        if args.command == "accounts":
            method, path, body = "GET", "/v1/accounts", None
        elif args.command == "execute":
            method, path, body = "POST", "/v1/execute", _json_file(args.file)
        else:
            method, path = args.method, args.path
            if not re.fullmatch(r"/v1/[A-Za-z0-9_/-]+", path) or "//" in path:
                raise ValueError("invalid API path")
            body = _json_file(args.file) if args.file else None
        status, response = asyncio.run(client.request(method, path, body))
        print(json.dumps(response, ensure_ascii=False, indent=2))
        if status >= 400:
            raise SystemExit(1)
    except (ValueError, OSError, KeyError):
        print("Invalid local configuration or input.", file=sys.stderr)
        raise SystemExit(2) from None
    except Exception:
        print("Gateway request failed; a mutation outcome may be unknown.", file=sys.stderr)
        raise SystemExit(1) from None


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
