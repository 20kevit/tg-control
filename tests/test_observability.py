"""Structured logging, metrics, tracing, health and diagnostics."""

import io
import json
import uuid

import pytest
from test_api import auth, create_resources, set_policy

from tg_control import observability
from tg_control.observability import (
    METRICS,
    SPANS,
    Metrics,
    configure_logging,
    log_event,
    safe_fields,
    span,
    trace_context,
)


@pytest.fixture(autouse=True)
def isolated_observability():
    METRICS.reset()
    SPANS.clear()
    yield
    METRICS.reset()
    SPANS.clear()


def read_logs():
    stream = io.StringIO()
    configure_logging("DEBUG", stream=stream)
    return stream


def test_structured_logs_are_json_bounded_and_redact_sensitive_fields():
    stream = read_logs()
    log_event(
        "http.request",
        method="POST",
        route="/v1/execute",
        status=200,
        owner_token="synthetic-owner-token-value",
        text="synthetic message body",
        phone_number="+12025550100",
        api_key="sk-synthetic",
        authorization="Bearer abc",
        nested={"credential": "x"},
        padding="p" * 5000,
    )
    configure_logging("INFO")
    line = stream.getvalue().strip().splitlines()[-1]
    record = json.loads(line)
    assert record["service"] == "tg-control" and record["v"] == 1
    assert record["event"] == "http.request"
    assert record["method"] == "POST" and record["status"] == 200
    for key in ("owner_token", "text", "phone_number", "api_key", "authorization"):
        assert record[key] == "[redacted]", key
    assert len(str(record["padding"])) <= 200
    assert len(line) <= observability.MAX_RECORD_CHARS
    assert "synthetic-owner-token-value" not in line
    assert "+12025550100" not in line
    assert "synthetic message body" not in line
    assert "\n" not in line


def test_log_event_never_raises_on_hostile_values():
    stream = read_logs()
    log_event("weird", value=b"\xff\xfe", other=float("nan"), nothing=None)
    configure_logging("INFO")
    assert json.loads(stream.getvalue().strip().splitlines()[-1])["event"] == "weird"


def test_safe_fields_bounds_everything():
    cleaned = safe_fields({"a": "x" * 1000, "secret_key": "value", "b": 5})
    assert cleaned["a"] == "x" * 200
    assert cleaned["secret_key"] == "[redacted]"
    assert cleaned["b"] == 5


def test_metrics_reject_unknown_labels_and_normalize_outcomes():
    metrics = Metrics()
    metrics.incr("tg_control_http_requests_total", method="GET", outcome="success")
    metrics.incr("tg_control_http_requests_total", method="GET", outcome="not-a-real-outcome")
    metrics.observe("tg_control_latency_seconds", 0.25, route="/v1/jobs")
    metrics.gauge("tg_control_active_jobs", 3, state="running")
    with pytest.raises(ValueError):
        metrics.incr("bad", user_supplied_label="secret")
    snapshot = metrics.snapshot()
    outcomes = {
        entry["labels"]["outcome"] for entry in snapshot["counters"].values()
    }
    assert outcomes == {"success", "error"}
    assert snapshot["histograms"][
        "tg_control_latency_seconds|route=/v1/jobs"
    ]["count"] == 1
    text = metrics.render_prometheus()
    assert 'tg_control_http_requests_total{method="GET",outcome="success"} 1' in text
    assert text.endswith("\n") and "tg_control_metric_series_dropped" in text


def test_metrics_cardinality_is_capped():
    metrics = Metrics(max_series=4)
    for index in range(20):
        metrics.incr("tg_control_test_total", route=f"/v1/route{index}", method="GET")
    snapshot = metrics.snapshot()
    assert len(snapshot["counters"]) <= 4
    assert snapshot["dropped_series"] >= 16
    assert metrics.render_prometheus().count("tg_control_test_total{") <= 4


async def test_spans_capture_hierarchy_duration_and_no_payloads():
    with trace_context("trace-synthetic"):
        with span("http.request", component="api", method="GET"):
            with span("core.execute", component="core", action="send", text="secret body"):
                pass
    recorded = SPANS.snapshot(limit=10)
    assert [item["name"] for item in recorded] == ["core.execute", "http.request"]
    inner, outer = recorded
    assert inner["parent_id"] == outer["span_id"]
    assert inner["trace_id"] == outer["trace_id"] == "trace-synthetic"
    assert inner["attributes"]["text"] == "[redacted]"
    assert inner["attributes"]["action"] == "send"
    assert all(item["duration_ms"] >= 0 for item in recorded)


async def test_span_records_failure_code_and_reraises():
    class Boom(Exception):
        code = "denied"

    with pytest.raises(Boom):
        with span("core.execute", component="core"):
            raise Boom()
    item = SPANS.snapshot(limit=1)[0]
    assert item["outcome"] == "denied" and item["error_code"] == "denied"


def test_health_ready_and_dependency_endpoints(api, settings):
    owner = auth(settings.owner_token)
    assert api.get("/health/live").status_code == 401
    live = api.get("/health/live", headers=owner)
    assert live.status_code == 200 and live.json()["checks"] == {"process": "ok"}
    ready = api.get("/health/ready", headers=owner)
    assert ready.status_code == 200, ready.text
    assert ready.json()["checks"]["database"] == "ok"
    assert ready.json()["checks"]["migration"] == "current"
    assert ready.json()["checks"]["instance_guard"] == "held"
    assert ready.json()["checks"]["scheduler"] == "running"
    dependencies = api.get("/health/dependencies", headers=owner).json()
    assert dependencies["database"]["state"] == "ok"
    assert dependencies["workers"]["scheduler"] == "running"
    assert "media_staging" in dependencies
    body = ready.text + dependencies.__str__()
    assert settings.owner_token not in body
    assert settings.encryption_key.decode() not in body
    assert "sqlite://" not in body


def test_metrics_traces_and_diagnostics_are_owner_only_and_bounded(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    api.get("/v1/accounts", headers=auth(token))
    for path in ("/v1/admin/metrics", "/v1/admin/traces", "/v1/admin/diagnostics"):
        assert api.get(path, headers=auth(token)).status_code == 401
    metrics_text = api.get("/v1/admin/metrics", headers=owner)
    assert metrics_text.status_code == 200
    assert "text/plain" in metrics_text.headers["content-type"]
    assert "tg_control_http_requests_total" in metrics_text.text
    as_json = api.get("/v1/admin/metrics?format=json", headers=owner).json()
    assert as_json["counters"] and as_json["dropped_series"] == 0
    traces = api.get("/v1/admin/traces?limit=5", headers=owner).json()
    assert len(traces["spans"]) <= 5
    assert all(
        set(span_item) >= {"name", "duration_ms", "outcome"} for span_item in traces["spans"]
    )
    diagnostics = api.get("/v1/admin/diagnostics", headers=owner)
    assert diagnostics.status_code == 200
    report = diagnostics.json()
    assert report["read_only"] is True
    assert report["database"]["migration_state"] == "current"
    assert {"jobs", "workflows", "events", "plugins", "accounts", "findings"} <= set(report)
    blob = json.dumps(report)
    assert settings.owner_token not in blob
    assert account["id"] not in blob  # no per-account identifiers, only aggregate states


def test_diagnostics_surface_failed_jobs_and_quarantined_plugins(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    manifest = api.post(
        "/v1/admin/plugins",
        headers=owner,
        json={
            "plugin_id": "firstparty.diag",
            "display_name": "Diagnostics plugin",
            "publisher": "tg-control first party",
            "version": "1.0.0",
            "sdk_range": ">=1.0,<2.0",
            "entry_point": "tg_control_firstparty.diag:run",
            "extension_points": ["worker"],
            "capabilities": ["read", "write"],
            "client_id": client["id"],
            "account_ids": [account["id"]],
        },
    )
    assert manifest.status_code == 201
    report = api.get("/v1/admin/diagnostics", headers=owner).json()
    assert report["plugins"]["installed"] == 1
    assert report["plugins"]["enabled"] == 0
    codes = {finding["code"] for finding in report["findings"]}
    assert isinstance(codes, set)


def test_request_id_is_echoed_and_correlates_errors(api, settings):
    owner = auth(settings.owner_token)
    supplied = str(uuid.uuid4())
    response = api.get("/v1/accounts", headers={**owner, "X-Request-ID": supplied})
    assert response.status_code == 401
    assert response.headers["x-request-id"] == supplied
    body = response.json()
    assert body["request_id"] == supplied
    assert body["status"] == 401 and body["retryable"] is False
    generated = api.get("/v1/accounts", headers=owner)
    assert generated.headers["x-request-id"] != supplied
    invalid = api.get("/v1/accounts", headers={**owner, "X-Request-ID": "bad id with spaces"})
    assert invalid.headers["x-request-id"] != "bad id with spaces"


def test_error_envelope_never_echoes_input(api, settings):
    owner = auth(settings.owner_token)
    marker = "synthetic-leak-marker-" + "z" * 40
    response = api.post("/v1/admin/clients", headers=owner, json={"name": marker * 5})
    assert response.status_code == 422
    body = response.json()
    assert set(body) >= {"error", "status", "request_id", "retryable", "outcome_unknown"}
    assert body["error"] == "invalid_request"
    assert marker not in response.text
    missing = api.get(f"/v1/admin/workflow-runs/{uuid.uuid4()}", headers=owner)
    assert missing.status_code == 404 and missing.json()["error"] == "run_not_found"
    assert missing.json()["outcome_unknown"] is False


def test_log_level_is_configurable_and_bounded():
    import io
    import logging

    import pytest
    from cryptography.fernet import Fernet

    from tg_control.config import Settings
    from tg_control.observability import configure_logging, log_event

    with pytest.raises(ValueError):
        Settings(
            "sqlite:///synthetic.sqlite",
            "synthetic-owner-token-" + "x" * 32,
            Fernet.generate_key(),
            log_level="DEBUG",
        )
    with pytest.raises(ValueError):
        Settings(
            "sqlite:///synthetic.sqlite",
            "synthetic-owner-token-" + "x" * 32,
            Fernet.generate_key(),
            log_level="verbose",
        )

    buffer = io.StringIO()
    configure_logging("WARNING", stream=buffer)
    log_event("synthetic.info", level=logging.INFO, outcome="success")
    log_event("synthetic.warning", level=logging.WARNING, outcome="error")
    emitted = buffer.getvalue().splitlines()
    assert len(emitted) == 1
    assert "synthetic.warning" in emitted[0]
    # Restore the default level for later tests in the same process.
    configure_logging("INFO")
