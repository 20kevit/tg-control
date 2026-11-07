"""Local MCP stdio server. It only talks to the authenticated gateway HTTP API."""

from __future__ import annotations

import os

import anyio
from mcp.server.stdio import stdio_server

from .config import _read_secret
from .http_client import GatewayHttpClient
from .mcp_server import HttpBackend, create_mcp_server


def main() -> None:
    token = _read_secret(os.environ["TG_CLIENT_TOKEN_FILE"])
    url = os.environ.get("TG_API_URL", "http://127.0.0.1:8000")
    server = create_mcp_server(HttpBackend(GatewayHttpClient(url, token)))

    async def run() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())

    anyio.run(run)


if __name__ == "__main__":
    main()
