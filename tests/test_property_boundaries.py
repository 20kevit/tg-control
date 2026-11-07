"""Seeded property-style tests for security and observability boundaries.

No property-testing dependency is installed (the dependency lock is deliberate),
so each invariant is exercised over a deterministic pseudo-random corpus with a
fixed seed. A failure prints the seed-reproducible case.
"""

import json
import logging
import random
import string

import pytest

from tg_control.errors import SAFE_MESSAGES, normalise_code
from tg_control.observability import ALLOWED_OUTCOMES, Metrics
from tg_control.pagination import CursorError, decode_cursor, derive_key, encode_cursor
from tg_control.throttle import MAX_TRACKED_SOURCES, AuthThrottle

SAFE_CHARS = set(string.ascii_lowercase + string.digits + "_-")
KEY = derive_key(b"property-test-key-material")


def test_error_codes_are_always_safe_tokens():
    rng = random.Random(20261107)
    alphabet = string.printable + "\u2603\u0000"
    for _ in range(300):
        hostile = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 80)))
        code = normalise_code(hostile)
        assert code
        assert len(code) <= 64
        assert set(code) <= SAFE_CHARS
        assert code in SAFE_MESSAGES or set(code) <= SAFE_CHARS
        assert "\n" not in code and "\x00" not in code


def test_cursor_round_trip_and_single_character_tamper_rejection():
    rng = random.Random(7)
    for _ in range(200):
        identifier = "".join(rng.choice(string.ascii_lowercase + string.digits + "-")
                             for _ in range(rng.randint(1, 36)))
        sort_value = rng.randint(0, 2**31)
        cursor = encode_cursor(
            key=KEY,
            scope="jobs:client-a",
            sort_value=sort_value,
            identifier=identifier,
            ttl_seconds=600,
            now=1_000_000,
        )
        assert decode_cursor(cursor, key=KEY, scope="jobs:client-a", now=1_000_001) == (
            sort_value,
            identifier,
        )
        # Any well-formed single-character edit is either rejected or decodes to
        # different content; it never authenticates as the original value.
        index = rng.randrange(len(cursor))
        replacement = rng.choice(string.ascii_letters + string.digits + ".-_=")
        if replacement == cursor[index]:
            continue
        tampered = cursor[:index] + replacement + cursor[index + 1 :]
        try:
            decoded = decode_cursor(tampered, key=KEY, scope="jobs:client-a", now=1_000_001)
        except CursorError:
            continue
        assert decoded != (sort_value, identifier) or replacement == cursor[index]


def test_cursor_never_authenticates_for_a_different_scope():
    rng = random.Random(11)
    for _ in range(100):
        scope = "".join(rng.choice(string.ascii_letters) for _ in range(rng.randint(1, 40)))
        cursor = encode_cursor(
            key=KEY,
            scope=scope,
            sort_value=1,
            identifier="synthetic-id",
            now=1_000_000,
        )
        assert decode_cursor(cursor, key=KEY, scope=scope, now=1_000_000) == (1, "synthetic-id")
        with pytest.raises(CursorError):
            decode_cursor(cursor, key=KEY, scope=scope + "-other", now=1_000_000)
        with pytest.raises(CursorError):
            decode_cursor(cursor, key=derive_key(b"another-key"), scope=scope, now=1_000_000)


def test_metric_labels_and_outcomes_are_bounded():
    rng = random.Random(99)
    metrics = Metrics(max_series=32)
    for _ in range(500):
        name = "synthetic_metric_total"
        metrics.incr(name, outcome=rng.choice(sorted(ALLOWED_OUTCOMES) + ["evil=<script>"]))
        metrics.observe(name, rng.random(), route="/v1/synthetic/{id}")
        metrics.gauge(name, rng.randint(0, 10), component="jobs")
        assert len(metrics.snapshot()["counters"]) <= 32
        assert len(metrics.snapshot()["gauges"]) <= 32
    rendered = metrics.render_prometheus()
    assert "evil" not in rendered
    assert "<script>" not in rendered
    with pytest.raises(ValueError):
        metrics.incr("synthetic_metric_total", account_id="should-not-be-a-label")


def test_json_log_redaction_with_hostile_nested_values():
    import io
    import sys

    from tg_control.observability import configure_logging, log_event

    buffer = io.StringIO()
    configure_logging("INFO", stream=buffer)
    rng = random.Random(5)
    sentinel = "synthetic-secret-sentinel-9f3"
    hostile_keys = ["token", "authorization", "password", "text", "message", "content"]
    for _ in range(50):
        payload = {
            rng.choice(hostile_keys): sentinel,
            "nested": {"deeper": [{rng.choice(hostile_keys): sentinel}]},
            "number": rng.random(),
            "weird": "\u0000\ud7ff",
        }
        log_event("synthetic.hostile", level=logging.INFO, outcome="success", extra=payload)
    output = buffer.getvalue()
    assert output
    for line in output.splitlines():
        assert sentinel not in line
        assert json.loads(line)["event"] == "synthetic.hostile"
    configure_logging("INFO", stream=sys.stderr)


def test_throttle_invariants_hold_for_random_sequences():
    rng = random.Random(3)
    clock_value = [1000.0]
    throttle = AuthThrottle(max_failures=3, window_seconds=10, lockout_seconds=20,
                            clock=lambda: clock_value[0])
    failures: dict[str, int] = {}
    for _ in range(400):
        source = f"source-{rng.randint(0, 20)}"
        clock_value[0] += rng.choice([0.0, 0.5, 2.0, 15.0])
        blocked = throttle.retry_after(source) is not None
        if blocked:
            assert failures.get(source, 0) >= 3
            continue
        if rng.random() < 0.8:
            throttle.failure(source)
            failures[source] = failures.get(source, 0) + 1
        else:
            throttle.success(source)
            failures[source] = 0
    assert len(throttle._state) <= MAX_TRACKED_SOURCES
