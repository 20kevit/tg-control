"""Pagination glue shared by the HTTP router and the MCP backend.

Keeping it here (rather than duplicating the keyset logic per interface) is what
makes cursor semantics identical across transports.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .pagination import CursorError, decode_cursor, page_response, scope_digest


def decode_before(scope: str, cursor: str | None, *, key: bytes):
    if cursor is None:
        return None
    sort_value, identifier = decode_cursor(cursor, key=key, scope=scope)
    try:
        parsed = datetime.fromisoformat(str(sort_value))
    except ValueError:
        raise CursorError("invalid_cursor") from None
    return parsed, identifier


def job_page(jobs, principal, limit: int, cursor: str | None, *, key: bytes) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= 100:
        raise CursorError("invalid_cursor")
    scope = scope_digest("jobs", "admin" if principal is None else principal.client_id)
    rows = jobs.list_jobs(principal, limit=limit + 1, before=decode_before(scope, cursor, key=key))
    page = page_response(
        items=rows,
        limit=limit,
        key=key,
        scope=scope,
        sort_value=lambda row: row["created_at"],
        identifier=lambda row: row["job_id"],
    )
    return {"jobs": page["items"], "page": page["page"]}
