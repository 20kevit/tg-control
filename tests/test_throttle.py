"""Authentication throttling: bounded, fail-closed, and never keyed by a token."""

import pytest

from tg_control.throttle import MAX_TRACKED_SOURCES, AuthThrottle


def auth(token):
    return {"Authorization": "Bearer " + token}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def test_throttle_locks_out_then_recovers_after_the_window():
    clock = Clock()
    throttle = AuthThrottle(max_failures=3, window_seconds=10, lockout_seconds=30, clock=clock)
    for _ in range(2):
        throttle.failure("10.0.0.7")
        assert throttle.retry_after("10.0.0.7") is None
    throttle.failure("10.0.0.7")
    assert throttle.retry_after("10.0.0.7") == pytest.approx(30.0)
    assert throttle.retry_after("10.0.0.8") is None
    clock.advance(29)
    assert throttle.retry_after("10.0.0.7") is not None
    clock.advance(2)
    assert throttle.retry_after("10.0.0.7") is None
    # A successful authentication clears the history for that source only.
    throttle.failure("other")
    throttle.success("other")
    assert throttle.retry_after("other") is None


def test_throttle_keys_are_digests_and_bounded():
    throttle = AuthThrottle(max_failures=2, clock=lambda: 1.0)
    assert "token" not in throttle.key("secret-token-value")
    assert len(throttle.key("secret-token-value")) == 32
    for index in range(MAX_TRACKED_SOURCES + 50):
        throttle.failure(f"source-{index}")
    assert len(throttle._state) <= MAX_TRACKED_SOURCES


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_failures": 0},
        {"window_seconds": 0},
        {"lockout_seconds": 10_000},
        {"max_failures": 100_000},
    ],
)
def test_throttle_configuration_is_bounded(kwargs):
    with pytest.raises(ValueError):
        AuthThrottle(**kwargs)


def test_api_throttles_repeated_invalid_credentials_with_retry_after(api, settings):
    for _ in range(20):
        response = api.get("/v1/accounts", headers=auth("x" * 40))
        assert response.status_code == 401, response.text
    blocked = api.get("/v1/accounts", headers=auth("x" * 40))
    assert blocked.status_code == 429, blocked.text
    body = blocked.json()
    assert body["error"] == "rate_limit"
    assert body["retryable"] is True
    assert body["outcome_unknown"] is False
    assert int(blocked.headers["Retry-After"]) >= 1
    # The block is evaluated before the credential, so even a valid token from
    # the same source is refused while the lockout holds (fail closed).
    assert api.get("/v1/accounts", headers=auth(settings.owner_token)).status_code == 429
    assert "invalid_credential" not in blocked.text


def test_a_valid_credential_resets_the_failure_history(api, settings):
    created = api.post(
        "/v1/admin/clients", headers=auth(settings.owner_token), json={"name": "throttled client"}
    )
    assert created.status_code == 201, created.text
    token = created.json()["credential"]
    for _ in range(19):
        assert api.get("/v1/accounts", headers=auth("y" * 40)).status_code == 401
    assert api.get("/v1/accounts", headers=auth(token)).status_code == 200
    for _ in range(19):
        assert api.get("/v1/accounts", headers=auth("z" * 40)).status_code == 401
    assert api.get("/v1/accounts", headers=auth(token)).status_code == 200


def test_auth_failures_and_lockouts_are_audited_without_the_credential(api, settings):
    from sqlalchemy import select

    from tg_control.db import AuditEvent, make_engine, make_sessions

    for _ in range(21):
        api.get("/v1/accounts", headers=auth("q" * 40))
    engine = make_engine(settings.database_url)
    sessions = make_sessions(engine)
    with sessions() as session:
        rows = session.scalars(select(AuditEvent)).all()
        payload = " ".join(
            f"{row.event} {row.decision} {row.error_code} {row.actor_id}" for row in rows
        )
    engine.dispose()
    assert "q" * 10 not in payload
    assert any(row.event == "authentication" and row.decision == "denied" for row in rows)
    assert any(row.error_code == "rate_limit" for row in rows)
