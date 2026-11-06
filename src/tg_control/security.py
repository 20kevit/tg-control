"""ASGI boundary: Origin checks, bounded bodies, safe error responses and headers."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class SecurityBoundary:
    def __init__(self, app: ASGIApp, *, allowed_origins: tuple[str, ...], max_bytes: int):
        self.app = app
        self.allowed_origins = set(allowed_origins)
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = scope.get("headers", [])
        for name in (b"host", b"origin", b"authorization", b"content-length"):
            if sum(1 for key, _ in headers if key.lower() == name) > 1:
                await self._error(send, 400, "invalid_headers")
                return
        origins = [value for key, value in headers if key.lower() == b"origin"]
        if origins:
            try:
                origin = origins[0].decode("ascii")
            except UnicodeDecodeError:
                await self._error(send, 403, "origin_denied")
                return
            if origin not in self.allowed_origins:
                await self._error(send, 403, "origin_denied")
                return
        lengths = [value for key, value in headers if key.lower() == b"content-length"]
        if lengths:
            try:
                length = int(lengths[0])
                if length < 0:
                    raise ValueError
            except ValueError:
                await self._error(send, 400, "invalid_headers")
                return
            if length > self.max_bytes:
                await self._error(send, 413, "request_too_large")
                return
        if scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                if message["type"] != "http.request":
                    continue
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > self.max_bytes:
                    await self._error(send, 413, "request_too_large")
                    return
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            body = b"".join(chunks)
            sent = False

            async def replay() -> Message:
                nonlocal sent
                if not sent:
                    sent = True
                    return {"type": "http.request", "body": body, "more_body": False}
                return await receive()

            inner_receive = replay
        else:
            inner_receive = receive
        started = False

        async def secure_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message.setdefault("headers", []).extend(
                    [
                        (b"cache-control", b"no-store"),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                        (b"x-frame-options", b"DENY"),
                        (
                            b"content-security-policy",
                            b"default-src 'self'; script-src 'self'; style-src 'self'; "
                            b"connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
                            b"form-action 'self'",
                        ),
                    ]
                )
            await send(message)

        try:
            await self.app(scope, inner_receive, secure_send)
        except Exception:
            # Never log raw exceptions or echo request content. A failed response after a
            # mutation may mean that it executed; clients must reconcile instead of retrying.
            if not started:
                await self._error(send, 500, "internal_error", outcome_unknown=True)

    @staticmethod
    async def _error(send: Send, status: int, code: str, *, outcome_unknown: bool = False) -> None:
        payload = {"error": code}
        if outcome_unknown:
            payload["outcome_unknown"] = True
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
