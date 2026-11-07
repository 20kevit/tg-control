"""Structured logging, tracing and bounded metrics.

Design rules (ADR-014):
- Logs are single-line JSON with a fixed envelope. No message text, prompt text,
  token, password, key, phone number or credential ever enters a log field: the
  only values accepted are identifiers, route templates, statuses, durations and
  error codes. Sensitive-looking keys are redacted defensively anyway.
- Every record carries a correlation id (``trace_id``/``request_id``) so an HTTP
  request, its Core decision, adapter call, workflow step, job execution, plugin
  call and AI invocation can be joined without payloads.
- Metrics use a closed label vocabulary and a hard series cap, so a hostile
  caller cannot create unbounded cardinality. Labels never contain user text.
- Tracing keeps a bounded in-memory span buffer for diagnostics. Spans carry
  names, durations, outcomes and numeric attributes only.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
import time
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Annotated, Any, Iterator

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .request_audit import request_id_context

SERVICE_NAME = "tg-control"
SCHEMA_VERSION = 1
MAX_FIELD_CHARS = 200
MAX_RECORD_CHARS = 4096
MAX_SERIES = 2048
MAX_SPANS = 512
REDACTED = "[redacted]"
SENSITIVE_KEY_PARTS = (
    "text",
    "message",
    "content",
    "body",
    "caption",
    "authorization",
    "cookie",
    "credential",
    "token",
    "secret",
    "password",
    "passwd",
    "phone",
    "encryption",
    "session",
    "private_key",
    "api_key",
)
# Only these label names may appear on a metric.
ALLOWED_LABELS = frozenset(
    {"method", "route", "status_class", "outcome", "kind", "state", "component"}
)
ALLOWED_OUTCOMES = frozenset(
    {
        "success",
        "failure",
        "denied",
        "not_found",
        "invalid",
        "conflict",
        "rate_limited",
        "timeout",
        "cancelled",
        "outcome_unknown",
        "retried",
        "error",
        "running",
        "succeeded",
        "partial_failure",
        "quarantined",
        "skipped",
    }
)

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)
_span_stack: ContextVar[tuple[str, ...]] = ContextVar("span_stack", default=())
_configured = False
_logger = logging.getLogger("tg_control")


def configure_logging(level: str = "INFO", *, stream=None) -> None:
    """Install the JSON formatter once. Repeated calls are safe."""
    global _configured
    numeric = getattr(logging, str(level).upper(), None)
    if type(numeric) is not int:
        numeric = logging.INFO
    if _configured:
        _logger.setLevel(numeric)
        if stream is not None and getattr(_logger.handlers[0], "stream", None) is not stream:
            _logger.handlers[0].setStream(stream)
        return
    handler = logging.StreamHandler(stream or sys.stderr)
    handler.setFormatter(_JsonFormatter())
    _logger.handlers.clear()
    _logger.addHandler(handler)
    _logger.setLevel(numeric)
    _logger.propagate = False
    _configured = True


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        envelope: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": SERVICE_NAME,
            "v": SCHEMA_VERSION,
            "event": getattr(record, "event", record.name),
            "trace_id": getattr(record, "trace_id", None) or _trace_id.get(),
        }
        for key, value in (getattr(record, "fields", None) or {}).items():
            envelope[key] = value
        if record.exc_info:  # Never serialise an exception message or stack text.
            envelope["exception"] = "unhandled"
        try:
            text = json.dumps(envelope, ensure_ascii=False, allow_nan=False, default=str)
        except (TypeError, ValueError):
            text = json.dumps({"ts": envelope["ts"], "level": envelope["level"],
                               "service": SERVICE_NAME, "event": "log.serialisation_failed"})
        return text[:MAX_RECORD_CHARS]


def _clean_value(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    text = str(value)
    return text[:MAX_FIELD_CHARS]


def safe_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Redact sensitive keys and bound every value. Never raises."""
    cleaned: dict[str, Any] = {}
    for key, value in fields.items():
        name = str(key)[:64]
        lowered = name.lower()
        if any(part in lowered for part in SENSITIVE_KEY_PARTS):
            cleaned[name] = REDACTED
        else:
            cleaned[name] = _clean_value(value)
    return cleaned


def log_event(event: str, *, level: int = logging.INFO, **fields: Any) -> None:
    _logger.log(level, event, extra={"event": str(event)[:80], "fields": safe_fields(fields)})


def current_trace_id() -> str | None:
    return _trace_id.get()


@contextmanager
def trace_context(trace_id: str) -> Iterator[None]:
    token = _trace_id.set(str(trace_id)[:64])
    try:
        yield
    finally:
        _trace_id.reset(token)


class RecordedSpan(dict):
    """A dict subclass so tests can use either attribute-free dict access."""


class SpanCollector:
    def __init__(self, capacity: int = MAX_SPANS):
        self._spans: deque[dict[str, Any]] = deque(maxlen=capacity)

    def append(self, span: dict[str, Any]) -> None:
        self._spans.append(span)

    def snapshot(self, limit: int = 100) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= MAX_SPANS:
            limit = 100
        return list(self._spans)[-limit:]

    def clear(self) -> None:
        self._spans.clear()


SPANS = SpanCollector()


@contextmanager
def span(name: str, *, component: str | None = None, **attributes: Any) -> Iterator[dict]:
    """Record one traced operation. Payload-free by construction."""
    parent = _span_stack.get()
    span_id = f"{len(parent) + 1}-{int(time.perf_counter() * 1000) % 100000}"
    record: dict[str, Any] = {
        "name": str(name)[:64],
        "component": str(component or "core")[:32],
        "trace_id": _trace_id.get(),
        "parent_id": parent[-1] if parent else None,
        "span_id": span_id,
        "started_at": time.time(),
        "attributes": safe_fields(attributes),
    }
    token = _span_stack.set((*parent, span_id))
    started = time.perf_counter()
    try:
        yield record
        record.setdefault("outcome", "success")
    except BaseException as exc:  # record the outcome, then re-raise unchanged
        record["outcome"] = getattr(exc, "code", None) or "error"
        record["error_code"] = str(getattr(exc, "code", "internal_error"))[:64]
        raise
    finally:
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 3)
        record.setdefault("outcome", "error")
        _span_stack.reset(token)
        SPANS.append(record)
        log_event(
            record["name"],
            level=logging.DEBUG,
            span_id=span_id,
            parent_id=record["parent_id"],
            duration_ms=record["duration_ms"],
            outcome=record["outcome"],
            component=record["component"],
        )


class Metrics:
    """Counters, gauges and histograms with an enforced series cap."""

    def __init__(self, max_series: int = MAX_SERIES):
        self._counters: dict[str, tuple[dict[str, Any], float]] = {}
        self._gauges: dict[str, tuple[dict[str, Any], float]] = {}
        self._histograms: dict[str, tuple[dict[str, Any], list[float]]] = {}
        self._dropped = 0
        self._max_series = max_series

    @staticmethod
    def _labels(labels: dict[str, Any]) -> dict[str, str]:
        clean: dict[str, str] = {}
        for key, value in labels.items():
            if key not in ALLOWED_LABELS:
                raise ValueError("metric label not permitted")
            text = str(value)
            if key == "outcome" and text not in ALLOWED_OUTCOMES:
                text = "error"
            clean[key] = text[:48]
        return dict(sorted(clean.items()))

    def _series(self, store: dict, name: str, labels: dict[str, str]):
        key = name + "|" + "|".join(f"{k}={v}" for k, v in labels.items())
        if key in store:
            return store[key]
        if len(store) >= self._max_series:
            self._dropped += 1
            return None
        value = {"count": 0.0} if store is self._counters else {"sum": 0.0, "count": 0}
        store[key] = (labels, value)
        return store[key]

    def incr(self, name: str, amount: float = 1.0, **labels: Any) -> None:
        clean = self._labels(labels)
        entry = self._series(self._counters, name, clean)
        if entry is not None:
            entry[1]["count"] += float(amount)

    def observe(self, name: str, value: float, **labels: Any) -> None:
        clean = self._labels(labels)
        entry = self._series(self._histograms, name, clean)
        if entry is not None:
            bucket = entry[1]
            bucket["sum"] += float(value)
            bucket["count"] += 1
            if len(bucket.setdefault("samples", [])) < 256:
                bucket["samples"].append(float(value))

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        clean = self._labels(labels)
        key = name + "|" + "|".join(f"{k}={v}" for k, v in clean.items())
        if key not in self._gauges and len(self._gauges) >= self._max_series:
            self._dropped += 1
            return
        self._gauges[key] = (clean, float(value))

    def snapshot(self) -> dict[str, Any]:
        return {
            "counters": {
                key: {"labels": labels, "value": value["count"]}
                for key, (labels, value) in sorted(self._counters.items())
            },
            "gauges": {
                key: {"labels": labels, "value": value}
                for key, (labels, value) in sorted(self._gauges.items())
            },
            "histograms": {
                key: {
                    "labels": labels,
                    "count": value["count"],
                    "sum": round(value["sum"], 6),
                    "avg": round(value["sum"] / value["count"], 6) if value["count"] else 0,
                }
                for key, (labels, value) in sorted(self._histograms.items())
            },
            "dropped_series": self._dropped,
        }

    def reset(self) -> None:
        self._counters.clear()
        self._gauges.clear()
        self._histograms.clear()
        self._dropped = 0

    def render_prometheus(self) -> str:
        lines: list[str] = []
        for key, (labels, value) in sorted(self._counters.items()):
            name = key.split("|", 1)[0]
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name}{self._format_labels(labels)} {value['count']:g}")
        for key, (labels, value) in sorted(self._gauges.items()):
            name = key.split("|", 1)[0]
            lines.append(f"# TYPE {name} gauge")
            lines.append(f"{name}{self._format_labels(labels)} {value:g}")
        for key, (labels, value) in sorted(self._histograms.items()):
            name = key.split("|", 1)[0]
            lines.append(f"# TYPE {name} summary")
            lines.append(f"{name}_count{self._format_labels(labels)} {value['count']:g}")
            lines.append(f"{name}_sum{self._format_labels(labels)} {value['sum']:g}")
        lines.append("# TYPE tg_control_metric_series_dropped gauge")
        lines.append(f"tg_control_metric_series_dropped {self._dropped}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _format_labels(labels: dict[str, str]) -> str:
        if not labels:
            return ""
        inner = ",".join(f'{key}="{value}"' for key, value in labels.items())
        return "{" + inner + "}"


METRICS = Metrics()


def observe_http_request(
    *, method: str, route: str, status_code: int, duration_seconds: float
) -> None:
    status_class = f"{status_code // 100}xx"
    outcome = "success" if status_code < 400 else "failure"
    METRICS.incr(
        "tg_control_http_requests_total",
        method=method,
        route=route,
        status_class=status_class,
        outcome=outcome,
    )
    METRICS.observe(
        "tg_control_http_request_duration_seconds",
        duration_seconds,
        method=method,
        route=route,
    )
    if status_code >= 500:
        METRICS.incr(
            "tg_control_http_errors_total",
            route=route,
            status_class=status_class,
            outcome="error",
        )


def record_component_outcome(component: str, outcome: str, *, kind: str = "operation") -> None:
    """Bounded component counter used by jobs, workflows, plugins and AI tasks."""
    METRICS.incr(
        "tg_control_component_outcomes_total",
        component=str(component)[:32],
        kind=str(kind)[:32],
        outcome=str(outcome)[:48] if str(outcome) in ALLOWED_OUTCOMES else "error",
    )


def normalise_route(route: str) -> str:
    """Use a route template, never a raw path, as a metric label."""
    text = str(route or "unknown")
    if not text.startswith("/"):
        text = "/" + text
    return text[:96]


def annotate_status(record: dict, status_code: int) -> dict:
    record["status_class"] = f"{status_code // 100}xx"
    return record


EventHandler = Annotated[str, "bounded event name"]


REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def valid_request_id(value: Any) -> bool:
    return type(value) is str and bool(REQUEST_ID_RE.match(value))


class ObservabilityMiddleware:
    """Outermost ASGI middleware: correlation id, span, structured log, metrics.

    It never reads a request body and never records a URL with identifiers.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        supplied = ""
        for key, value in scope.get("headers") or []:
            if key.lower() == b"x-request-id":
                supplied = value.decode("latin-1", "ignore")
                break
        request_id = supplied if valid_request_id(supplied) else str(uuid.uuid4())
        state = scope.setdefault("state", {})
        if not isinstance(state, dict):  # pragma: no cover - ASGI contract
            state = {}
            scope["state"] = state
        state["request_id"] = request_id
        method = str(scope.get("method", ""))[:8]
        status_code = 500
        started = time.perf_counter()

        async def send_with_header(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                headers = [
                    (key, value)
                    for key, value in (message.get("headers") or [])
                    if key.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        request_token = request_id_context.set(request_id)
        trace_token = _trace_id.set(request_id)
        try:
            with span("http.request", component="api", method=method) as record:
                try:
                    await self.app(scope, receive, send_with_header)
                finally:
                    route = getattr(scope.get("route"), "path", None) or _static_route(
                        scope.get("path", "")
                    )
                    record["route"] = normalise_route(route)
                    record["status_class"] = f"{status_code // 100}xx"
                    record["outcome"] = "success" if status_code < 400 else "failure"
        finally:
            duration = time.perf_counter() - started
            route_template = normalise_route(
                getattr(scope.get("route"), "path", None) or _static_route(scope.get("path", ""))
            )
            observe_http_request(
                method=method,
                route=route_template,
                status_code=status_code,
                duration_seconds=duration,
            )
            log_event(
                "http.request",
                method=method,
                route=route_template,
                status=status_code,
                status_class=f"{status_code // 100}xx",
                duration_ms=round(duration * 1000, 3),
                outcome="success" if status_code < 400 else "failure",
            )
            _trace_id.reset(trace_token)
            request_id_context.reset(request_token)


def _static_route(path: str) -> str:
    """Bound a label for unmatched paths without using caller-controlled text."""
    text = str(path or "")
    if text in {"/", "/health/live", "/health/ready", "/health/dependencies", "/metrics"}:
        return text
    if text.startswith("/v1/") and text.count("/") <= 4 and text[3:].replace("/", "").isalpha():
        return text
    return "/unmatched"
