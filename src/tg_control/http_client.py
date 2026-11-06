"""Minimal client for the shared gateway API, used by CLI and local MCP."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx


def validate_base_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "gateway URL must be an origin without credentials, path, query or fragment"
        )
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("non-loopback gateway connections require HTTPS")
    return url.rstrip("/")


class GatewayHttpClient:
    def __init__(self, base_url: str, token: str, *, transport=None):
        self.base_url = validate_base_url(base_url)
        self.token = token
        self.transport = transport

    async def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        # Paths are controlled by this client, never by content received from Telegram.
        async with httpx.AsyncClient(
            base_url=self.base_url,
            transport=self.transport,
            timeout=60,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            response = await client.request(
                method, path, headers={"Authorization": f"Bearer {self.token}"}, json=body
            )
            try:
                data = response.json()
                if not isinstance(data, dict):
                    data = {"error": "invalid_gateway_response"}
            except ValueError:
                data = {"error": "invalid_gateway_response"}
            return response.status_code, data
