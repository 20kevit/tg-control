"""Minimal client for the shared gateway API, used by CLI and local MCP."""

from __future__ import annotations

from urllib.parse import urlsplit

import httpx


def valid_batch_items(result: dict, operations: list[str]) -> bool:
    """Check that a successful batch response accounts for every requested action."""
    items = result.get("items")
    if not operations or not isinstance(items, list) or len(items) != len(operations):
        return False
    for index, (item, operation) in enumerate(zip(items, operations)):
        if (
            not isinstance(item, dict)
            or type(item.get("index")) is not int
            or item["index"] != index
            or type(operation) is not str
            or item.get("operation") != operation
        ):
            return False
        if item.get("status") == "success":
            if (
                not isinstance(item.get("data"), dict)
                or "error" in item
                or "outcome_unknown" in item
            ):
                return False
        elif item.get("status") == "failure":
            if (
                type(item.get("error")) is not str
                or not item["error"]
                or type(item.get("outcome_unknown")) is not bool
                or "data" in item
            ):
                return False
        else:
            return False
    return True


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

    async def request(
        self, method: str, path: str, body: dict | None = None
    ) -> tuple[int, dict | str]:
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
            content_type = response.headers.get("content-type", "")
            if "application/json" in content_type:
                try:
                    data = response.json()
                except ValueError:
                    return response.status_code, {"error": "invalid_gateway_response"}
                if not isinstance(data, dict):
                    return response.status_code, {"error": "invalid_gateway_response"}
                return response.status_code, data
            if (
                200 <= response.status_code < 300
                and content_type.split(";")[0].strip() == "text/plain"
            ):
                # Prometheus text exposition is the only non-JSON success body.
                return response.status_code, response.text[:1_048_576]
            return response.status_code, {"error": "invalid_gateway_response"}
