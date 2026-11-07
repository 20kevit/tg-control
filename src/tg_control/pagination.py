"""Opaque, signed, scope-bound keyset pagination.

Guarantees:
- Stable ordering: callers page by an immutable sort key plus a unique
  tiebreaker, so concurrent inserts or deletions cannot make an item appear
  twice or vanish silently inside one page walk.
- Deterministic cursors: the same position always encodes to the same token.
- Opaque and tamper-evident: the payload is base64url JSON authenticated with
  HMAC-SHA256 over a key derived from the application's key material. A modified
  token, a token from another scope/principal, or a token from another filter
  fails closed with ``invalid_cursor``.
- Bounded: page size is validated, cursors are length-limited, and optional
  expiry reports ``expired_cursor``.
- No data leakage: the payload contains only a sort key and a tiebreaker id,
  never message content, credentials or another client's identifiers.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import re
import time
from typing import Any

MAX_CURSOR_LENGTH = 512
MAX_LIMIT = 100
DEFAULT_LIMIT = 25
CURSOR_VERSION = 1


class CursorError(Exception):
    """Normalized cursor failure; never carries the offending token."""

    def __init__(self, code: str = "invalid_cursor"):
        self.code = code if code in {"invalid_cursor", "expired_cursor"} else "invalid_cursor"
        super().__init__(self.code)


def derive_key(key_material: bytes) -> bytes:
    if not isinstance(key_material, (bytes, bytearray)) or not key_material:
        raise ValueError("cursor key material is required")
    return hashlib.sha256(b"tg-control:cursor:v1:" + bytes(key_material)).digest()


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    if not text or "=" in text:
        raise ValueError("non-canonical base64")
    padding = "=" * (-len(text) % 4)
    raw = base64.b64decode(text + padding, altchars=b"-_", validate=True)
    if _b64encode(raw) != text:  # reject non-canonical encodings outright
        raise ValueError("non-canonical base64")
    return raw


def normalise_limit(limit: Any, *, default: int = DEFAULT_LIMIT, maximum: int = MAX_LIMIT) -> int:
    if limit is None:
        return default
    if type(limit) is not int or limit < 1 or limit > maximum:
        raise CursorError("invalid_cursor")
    return limit


def encode_cursor(
    *,
    key: bytes,
    scope: str,
    sort_value: Any,
    identifier: Any,
    ttl_seconds: int | None = None,
    now: float | None = None,
) -> str:
    if not isinstance(scope, str) or not scope or len(scope) > 128:
        raise ValueError("cursor scope is required")
    if identifier is None:
        raise ValueError("cursor identifier is required")
    issued = int(now if now is not None else time.time())
    payload: dict[str, Any] = {
        "v": CURSOR_VERSION,
        "s": scope,
        "o": sort_value,
        "i": identifier,
        "t": issued,
    }
    if ttl_seconds is not None:
        if type(ttl_seconds) is not int or not 60 <= ttl_seconds <= 30 * 24 * 3600:
            raise ValueError("invalid cursor ttl")
        payload["e"] = issued + ttl_seconds
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(key, raw, hashlib.sha256).digest()[:16]
    return _b64encode(raw) + "." + _b64encode(signature)


def decode_cursor(
    cursor: Any,
    *,
    key: bytes,
    scope: str,
    now: float | None = None,
) -> tuple[Any, Any]:
    """Return (sort_value, identifier) or raise ``CursorError``."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise CursorError("invalid_cursor")
    if cursor.count(".") != 1:
        raise CursorError("invalid_cursor")
    encoded_payload, encoded_signature = cursor.split(".", 1)
    try:
        raw = _b64decode(encoded_payload)
        signature = _b64decode(encoded_signature)
    except (binascii.Error, ValueError):
        raise CursorError("invalid_cursor") from None
    if len(raw) > 1024 or len(signature) != 16:
        raise CursorError("invalid_cursor")
    expected = hmac.new(key, raw, hashlib.sha256).digest()[:16]
    if not hmac.compare_digest(signature, expected):
        raise CursorError("invalid_cursor")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise CursorError("invalid_cursor") from None
    if not isinstance(payload, dict) or payload.get("v") != CURSOR_VERSION:
        raise CursorError("invalid_cursor")
    if payload.get("s") != scope:
        # A cursor minted for another principal/filter is not usable.
        raise CursorError("invalid_cursor")
    identifier = payload.get("i")
    if type(identifier) is int:
        if identifier <= 0:
            raise CursorError("invalid_cursor")
    elif type(identifier) is str:
        if not 1 <= len(identifier) <= 64 or not re.fullmatch(r"[A-Za-z0-9-]+", identifier):
            raise CursorError("invalid_cursor")
    else:
        raise CursorError("invalid_cursor")
    expiry = payload.get("e")
    if expiry is not None:
        if type(expiry) is not int:
            raise CursorError("invalid_cursor")
        if (now if now is not None else time.time()) >= expiry:
            raise CursorError("expired_cursor")
    return payload.get("o"), identifier


def page_response(
    *,
    items: list[dict[str, Any]],
    limit: int,
    key: bytes,
    scope: str,
    sort_value: Any,
    identifier: Any,
    ttl_seconds: int | None = None,
) -> dict[str, Any]:
    has_more = len(items) > limit
    visible = items[:limit]
    next_cursor = None
    if has_more and visible:
        last = visible[-1]
        next_cursor = encode_cursor(
            key=key,
            scope=scope,
            sort_value=sort_value(last),
            identifier=identifier(last),
            ttl_seconds=ttl_seconds,
        )
    return {
        "items": visible,
        "page": {
            "limit": limit,
            "returned": len(visible),
            "has_more": has_more,
            "next_cursor": next_cursor,
        },
    }


def scope_digest(*parts: Any) -> str:
    """Stable short digest binding a cursor to principal and filter state."""
    material = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]
