"""Canonical client-facing error envelope.

Every error response produced by the HTTP API uses this shape:

    {
      "error": "<stable code>",
      "status": <http status>,
      "request_id": "<correlation id>",
      "retryable": <bool>,
      "outcome_unknown": <bool>,
      ...optional "retry_after" or "items"
    }

Rules:
- Stable codes only. Upstream text, SQL, file paths, stack traces and validation
  input are never echoed.
- ``retryable`` is advisory: it says the same request may be attempted again
  without changing intent. Mutations whose effect is unknown are never marked
  retryable and always set ``outcome_unknown``.
- ``request_id`` lets an operator correlate the client report with audit and
  trace records without exposing internals.
"""

from __future__ import annotations

from typing import Any

# Codes that are safe to retry as-is (nothing durable was applied or the
# condition is transient and a repeat is explicitly allowed).
RETRYABLE_CODES = frozenset(
    {
        "rate_limit",
        "rate_limited",
        "plugin_rate_limited",
        "storage_unavailable",
        "gateway_unavailable",
        "provider_unavailable",
        "timeout",
        "plugin_timeout",
        "reconnect_timeout",
        "rate_limit_timeout",
        "instance_lost",
        "busy",
    }
)
# Codes that mean the request may or may not have taken effect.
UNKNOWN_OUTCOME_CODES = frozenset(
    {
        "outcome_unknown",
        "storage_unavailable",
        "gateway_unavailable",
        "provider_error",
        "internal_error",
        "adapter_failure",
        "plugin_error",
        "upload_failed",
        "download_failed",
    }
)
SAFE_MESSAGES = frozenset(
    {
        "invalid_credential",
        "forbidden",
        "invalid_request",
        "not_found",
        "conflict",
        "storage_unavailable",
        "internal_error",
    }
)


def normalise_code(code: Any) -> str:
    text = str(code or "internal_error").strip().lower()
    if not text or len(text) > 64:
        return "internal_error"
    if any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in text):
        return "internal_error"
    return text


def retryable_code(code: str) -> bool:
    return normalise_code(code) in RETRYABLE_CODES


def unknown_outcome_code(code: str) -> bool:
    return normalise_code(code) in UNKNOWN_OUTCOME_CODES


def error_envelope(
    code: str,
    status_code: int,
    *,
    request_id: str | None,
    retryable: bool | None = None,
    outcome_unknown: bool | None = None,
    retry_after: float | int | None = None,
    items: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the canonical error body. Never includes exception text."""
    canonical = normalise_code(code)
    body: dict[str, Any] = {
        "error": canonical,
        "status": int(status_code),
        "request_id": str(request_id)[:64] if request_id else None,
        "retryable": retryable_code(canonical) if retryable is None else bool(retryable),
        "outcome_unknown": (
            unknown_outcome_code(canonical) or int(status_code) >= 500
            if outcome_unknown is None
            else bool(outcome_unknown)
        ),
    }
    if items is not None:
        body["items"] = _bounded_items(items)
    if retry_after is not None:
        try:
            body["retry_after"] = round(float(retry_after), 3)
        except (TypeError, ValueError):
            pass
    if extra:
        for key, value in extra.items():
            if key in body or not isinstance(key, str) or len(key) > 32:
                continue
            if isinstance(value, (str, int, float, bool)) or value is None:
                body[key] = value if not isinstance(value, str) else value[:200]
    return body


def _bounded_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    bounded: list[dict[str, Any]] = []
    for item in items[:100]:
        if not isinstance(item, dict):
            continue
        bounded.append(
            {
                key: (
                    value
                    if isinstance(value, (int, float, bool)) or value is None
                    else str(value)[:64] if key == "error" else str(value)[:120]
                )
                for key, value in item.items()
                if key in {"index", "operation", "status", "error"}
            }
        )
    return bounded


def validation_detail_allowed(detail: Any) -> bool:
    """Validation detail may name a field, never echo a supplied value."""
    text = str(detail)
    return text in SAFE_MESSAGES
