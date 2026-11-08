import json
import uuid

from sqlalchemy import select

from tg_control.db import Account, AuditEvent, Credential


def auth(token):
    return {"Authorization": "Bearer " + token}


def create_resources(api, settings):
    owner = auth(settings.owner_token)
    account = api.post(
        "/v1/admin/accounts",
        headers=owner,
        json={"alias": "Synthetic account", "phone_number": "+12025550100"},
    )
    assert account.status_code == 201, account.text
    client = api.post("/v1/admin/clients", headers=owner, json={"name": "synthetic client"})
    assert client.status_code == 201, client.text
    return owner, account.json(), client.json()


def set_policy(api, owner, account, client, policy=None):
    policy = policy or {
        "read": {"enabled": True, "allow_types": ["public_channel"]},
        "write": {"enabled": True, "allow_chats": ["public-channel"]},
        "admin": {"enabled": True, "allow_chats": ["private-group"]},
    }
    response = api.put(
        f"/v1/admin/clients/{client['id']}/accounts/{account['id']}/policy",
        headers=owner,
        json=policy,
    )
    assert response.status_code == 200, response.text


def execute(api, token, account, operation, chat=None, key=None, **fields):
    action = {"account_id": account["id"], "operation": operation, **fields}
    if chat:
        action["chat_id"] = chat
    body = {"actions": [action]}
    if key:
        body["idempotency_key"] = str(key)
    return api.post("/v1/execute", headers=auth(token), json=body)


def test_account_capabilities_route_is_authenticated_and_account_scoped(api, settings):
    owner, account, client = create_resources(api, settings)
    route = f"/v1/accounts/{account['id']}/capabilities"
    assert api.get(route, headers=owner).status_code == 401
    assert api.get(route, headers=auth(client["credential"])).status_code == 403
    set_policy(
        api,
        owner,
        account,
        client,
        policy={"read": {"enabled": True, "allow_types": ["public_channel"]}},
    )
    response = api.get(route, headers=auth(client["credential"]))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == account["id"]
    assert body["adapter_mode"] == "synthetic"
    assert body["live_adapter_enabled"] is False
    assert all(item["live_status"] == "blocked_compliance" for item in body["capabilities"])
    assert all(item["permission"] == "read" for item in body["capabilities"])
    operations = {item["operation"] for item in body["capabilities"]}
    assert "get_chat_info" in operations
    assert "send" not in operations
    assert "set_title" not in operations
    assert "private-group" not in str(body)
    assert "Synthetic private group" not in str(body)


def test_admin_capabilities_route_is_owner_only(api, settings):
    owner, account, client = create_resources(api, settings)
    route = f"/v1/admin/accounts/{account['id']}/capabilities"
    assert api.get(route, headers=auth(client["credential"])).status_code == 401
    response = api.get(route, headers=owner)
    assert response.status_code == 200, response.text
    assert response.json()["account_id"] == account["id"]
    assert response.json()["live_adapter_enabled"] is False
    assert len(response.json()["capabilities"]) == 147
    assert any(
        item["operation"] == "manage_star_subscription"
        for item in response.json()["capabilities"]
    )


def test_end_to_end_auth_scopes_credential_rotation_and_revocation(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    assert api.get("/v1/admin/clients", headers=auth(token)).status_code == 401
    assert api.get("/v1/accounts", headers=owner).status_code == 401
    assert api.get("/v1/accounts", headers=auth(token)).json() == {"accounts": []}
    set_policy(api, owner, account, client)
    assert api.get("/v1/accounts", headers=auth(token)).json()["accounts"][0]["id"] == account["id"]
    chats = execute(api, token, account, "list_chats").json()["items"][0]["data"]["chats"]
    assert [c["id"] for c in chats] == ["public-channel"]
    assert execute(api, token, account, "history", "private-direct").status_code == 403
    assert (
        execute(
            api, token, account, "send", "private-group", key=uuid.uuid4(), text="no"
        ).status_code
        == 403
    )
    message = execute(
        api, token, account, "send", "public-channel", key=uuid.uuid4(), text="synthetic text"
    )
    assert message.status_code == 200 and message.json()["items"][0]["status"] == "success"
    rotated = api.post(f"/v1/admin/clients/{client['id']}/rotate", headers=owner).json()[
        "credential"
    ]
    assert api.get("/v1/accounts", headers=auth(token)).status_code == 401
    assert api.get("/v1/accounts", headers=auth(rotated)).status_code == 200
    assert api.post(f"/v1/admin/clients/{client['id']}/revoke", headers=owner).status_code == 200
    assert api.get("/v1/accounts", headers=auth(rotated)).status_code == 401


def test_api_batch_preflight_and_immediate_policy_revocation(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    key = str(uuid.uuid4())
    payload = {
        "idempotency_key": key,
        "actions": [
            {
                "account_id": account["id"],
                "operation": "send",
                "chat_id": "public-channel",
                "text": "should not be sent",
            },
            {
                "account_id": account["id"],
                "operation": "forward",
                "chat_id": "public-channel",
                "source_chat_id": "private-direct",
                "message_id": 1,
            },
        ],
    }
    denied = api.post("/v1/execute", headers=auth(token), json=payload)
    assert denied.status_code == 403
    assert denied.json()["items"][0]["status"] == "not_executed"
    history = execute(api, token, account, "history", "public-channel").json()
    assert "should not be sent" not in json.dumps(history)
    set_policy(
        api,
        owner,
        account,
        client,
        {
            "read": {
                "enabled": True,
                "allow_types": ["public_channel"],
                "exclude_chats": ["public-channel"],
            }
        },
    )
    assert execute(api, token, account, "history", "public-channel").status_code == 403
    assert (
        execute(api, token, account, "search", query="fixture").json()["items"][0]["data"]["chats"]
        == []
    )


def test_input_origin_host_size_and_security_headers(api, settings):
    owner = auth(settings.owner_token)
    assert (
        api.get("/health/live", headers={**owner, "Origin": "https://evil.invalid"}).status_code
        == 403
    )
    assert api.get("/health/live", headers={**owner, "Host": "evil.invalid"}).status_code == 400
    ok = api.get("/health/live", headers={**owner, "Origin": "http://testserver"})
    assert ok.status_code == 200
    assert ok.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in ok.headers["content-security-policy"]
    assert (
        api.post(
            "/v1/admin/accounts", headers=owner, json={"alias": "x", "phone_number": "+12125551234"}
        ).status_code
        == 422
    )
    assert (
        api.post(
            "/v1/admin/accounts",
            headers=owner,
            json={"alias": "x", "phone_number": "+12025550100", "role": "owner"},
        ).status_code
        == 422
    )
    oversized = api.post(
        "/v1/admin/clients",
        headers={**owner, "Content-Type": "application/json"},
        content=b"x" * (settings.max_request_bytes + 1),
    )
    assert oversized.status_code == 413
    assert (
        api.get("/v1/admin/clients", headers={"Authorization": "Bearer invalid"}).status_code == 401
    )


def test_audit_and_at_rest_secrecy(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    payload = "synthetic secret-like message DO NOT INTERPRET"
    key = uuid.uuid4()
    sent = execute(
        api, client["credential"], account, "send", "public-channel", key=key, text=payload
    )
    assert sent.status_code == 200
    duplicate = execute(
        api, client["credential"], account, "send", "public-channel", key=key, text=payload
    )
    assert duplicate.status_code == 409
    events = api.get("/v1/admin/audit", headers=owner).json()["events"]
    assert any(
        e["event"] == "create_client" and e["subject_client_id"] == client["id"] for e in events
    )
    assert payload not in json.dumps(events)
    assert client["credential"] not in json.dumps(events)
    with api.app.state.sessions() as session:
        row = session.scalar(select(Account))
        assert "+12025550100" not in row.phone_encrypted
        assert all(
            client["credential"] not in c.token_digest for c in session.scalars(select(Credential))
        )
        assert all(payload not in str(vars(e)) for e in session.scalars(select(AuditEvent)))


def test_settings_session_health_and_owner_authority(api, settings):
    owner, account, client = create_resources(api, settings)
    assert api.get("/health/ready", headers=owner).json()["mode"] == "synthetic"
    assert (
        api.get(f"/v1/admin/accounts/{account['id']}/status", headers=owner).json()["state"]
        == "ready"
    )
    adapter = api.app.state.gateway.adapter
    adapter.set_state(account["id"], "reauth_required")
    status = api.get(f"/v1/admin/accounts/{account['id']}/status", headers=owner).json()
    assert status == {"state": "reauth_required", "intervention_required": True}
    reconnect = api.post(f"/v1/admin/accounts/{account['id']}/reconnect", headers=owner).json()
    assert reconnect == {
        "state": "reauth_required",
        "intervention_required": True,
        "outcome_unknown": False,
    }
    assert (
        api.get(
            f"/v1/admin/accounts/{account['id']}/status", headers=auth(client["credential"])
        ).status_code
        == 401
    )
    assert api.get("/v1/admin/settings", headers=owner).json()["audit_retention_days"] == 90
    new = {
        "audit_retention_days": 30,
        "cache_ttl_seconds": 60,
        "media_retention_days": None,
        "event_retention_days": 14,
    }
    # The response is the full persisted settings object (defaults included);
    # every submitted field must round-trip exactly.
    stored = api.put("/v1/admin/settings", headers=owner, json=new).json()
    assert stored.items() >= new.items()
    assert api.get("/v1/admin/settings", headers=owner).json() == stored
    assert stored["auto_prune"] is False  # automatic pruning stays opt-in
    assert (
        api.post(
            "/v1/admin/maintenance/prune-audit", headers=auth(client["credential"])
        ).status_code
        == 401
    )
    assert api.post("/v1/admin/maintenance/prune-audit", headers=owner).status_code == 200
    assert (
        api.patch(
            f"/v1/admin/accounts/{account['id']}", headers=owner, json={"enabled": False}
        ).status_code
        == 200
    )
    set_policy(api, owner, account, client)
    assert api.get("/v1/accounts", headers=auth(client["credential"])).json() == {"accounts": []}


def test_invalid_content_is_not_echoed(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    phrase = "secret-input-should-not-be-echoed"
    response = execute(api, client["credential"], account, "send", "public-channel", text=phrase)
    assert response.status_code == 422 and phrase not in response.text
    response = api.put(
        f"/v1/admin/clients/{client['id']}/accounts/{account['id']}/policy",
        headers=owner,
        json={"read": {"enabled": True, "allow_types": ["private_direct"]}},
    )
    assert response.status_code == 422


def test_chunked_body_limit_and_duplicate_sensitive_headers(api, settings):
    owner = auth(settings.owner_token)
    chunks = iter(
        [b"x" * (settings.max_request_bytes // 2 + 1), b"x" * (settings.max_request_bytes // 2 + 1)]
    )
    response = api.post("/v1/admin/clients", headers=owner, content=chunks)
    assert response.status_code == 413
    response = api.get(
        "/health/live",
        headers=[("Origin", "http://testserver"), ("Origin", "https://evil.invalid")],
    )
    assert response.status_code == 400


def test_health_and_openapi_require_fresh_credentials_and_are_audited(api, settings):
    owner, _account, client = create_resources(api, settings)
    token = client["credential"]
    for path in ("/health/live", "/health/ready", "/openapi.json"):
        assert api.get(path).status_code == 401
        assert api.get(path, headers=auth("invalid")).status_code == 401
        assert api.get(path, headers=owner).status_code == 200
    for path in ("/health/live", "/health/ready"):
        assert api.get(path, headers=auth(token)).status_code == 401
    schema = api.get("/openapi.json", headers=auth(token))
    assert schema.status_code == 200 and "/v1/execute" in schema.json()["paths"]
    assert api.post(f"/v1/admin/clients/{client['id']}/revoke", headers=owner).status_code == 200
    assert api.get("/openapi.json", headers=auth(token)).status_code == 401
    events = api.get("/v1/admin/audit", headers=owner).json()["events"]
    assert any(e["event"] == "schema_request" and e["actor_kind"] == "client" for e in events)
    assert any(e["event"] == "health_request" and e["decision"] == "success" for e in events)
    assert any(e["event"] == "authentication" and e["decision"] == "denied" for e in events)
    assert token not in json.dumps(events) and settings.owner_token not in json.dumps(events)


def test_health_fails_closed_if_audit_is_unavailable(api, settings, monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("synthetic-sensitive-exception")

    monkeypatch.setattr("tg_control.request_audit.audit.record", unavailable)
    response = api.get("/health/live", headers=auth(settings.owner_token))
    assert response.status_code == 503
    assert response.json() == {"error": "audit_unavailable", "outcome_unknown": False}
    assert "synthetic-sensitive-exception" not in response.text


def test_all_methods_enforce_streaming_body_and_framing_boundaries(api, settings):
    owner = auth(settings.owner_token)
    for method in ("GET", "OPTIONS"):
        chunks = iter(
            [
                b"x" * (settings.max_request_bytes // 2 + 1),
                b"x" * (settings.max_request_bytes // 2 + 1),
            ]
        )
        response = api.request(method, "/health/live", headers=owner, content=chunks)
        assert response.status_code == 413
    assert api.request("GET", "/health/live", headers=owner, content=b"x").status_code == 200
    response = api.request(
        "GET", "/health/live", headers={**owner, "Content-Length": "3"}, content=b"x"
    )
    assert response.status_code == 400
    response = api.request(
        "POST",
        "/v1/admin/clients",
        headers={**owner, "Content-Length": "1", "Transfer-Encoding": "chunked"},
        content=b"x",
    )
    assert response.status_code == 400
    response = api.request(
        "GET",
        "/health/live",
        headers=[
            ("Authorization", owner["Authorization"]),
            ("Transfer-Encoding", "chunked"),
            ("Transfer-Encoding", "chunked"),
        ],
        content=iter([b"x"]),
    )
    assert response.status_code == 400
    response = api.request("GET", "/health/live", headers={**owner, "Content-Length": "9" * 5000})
    assert response.status_code == 400 and "9" * 50 not in response.text
    events = api.get("/v1/admin/audit", headers=owner).json()["events"]
    assert any(
        event["event"] == "boundary_rejection" and event["error_code"] == "http_413"
        for event in events
    )


def test_sqlite_single_gateway_excludes_other_processes_and_releases(settings):
    import subprocess
    import sys

    import pytest
    from fastapi.testclient import TestClient
    from test_migrations import migrate

    from tg_control.api import create_app

    migrate(settings.database_url)
    child = """
import os, sys
from tg_control.db import make_engine
from tg_control.instance_guard import InstanceGuard
engine = make_engine(os.environ['SYNTHETIC_DB_URL'])
guard = InstanceGuard(engine)
try:
    guard.acquire()
except RuntimeError:
    sys.exit(23)
finally:
    guard.close()
    engine.dispose()
"""
    env = {"SYNTHETIC_DB_URL": settings.database_url}
    with TestClient(create_app(settings)) as first:
        with pytest.raises(RuntimeError, match="another gateway is already using"):
            with TestClient(create_app(settings)):
                pass
        blocked = subprocess.run(
            [sys.executable, "-I", "-c", child], env=env, capture_output=True, timeout=15
        )
        assert blocked.returncode == 23, blocked.stderr.decode()
        assert first.get("/health/ready", headers=auth(settings.owner_token)).status_code == 200
    released = subprocess.run(
        [sys.executable, "-I", "-c", child], env=env, capture_output=True, timeout=15
    )
    assert released.returncode == 0, released.stderr.decode()
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/health/ready", headers=auth(settings.owner_token)).status_code == 200


def test_sqlite_guard_loss_rejects_new_requests_and_stays_failed(settings, caplog):
    from fastapi.testclient import TestClient
    from test_migrations import migrate

    from tg_control.api import create_app

    migrate(settings.database_url)
    app = create_app(settings)
    with TestClient(app) as first:
        owner, account, client = create_resources(first, settings)
        set_policy(first, owner, account, client)
        # Simulate replacement of a lock file in this disposable synthetic fixture only.
        app.state.instance_guard._path.unlink()
        with TestClient(create_app(settings)) as second:
            assert second.get("/health/ready", headers=owner).status_code == 200
            denied = execute(
                first,
                client["credential"],
                account,
                "send",
                "public-channel",
                key=uuid.uuid4(),
                text="must not be sent",
            )
            assert denied.status_code == 503
            assert denied.json() == {"error": "instance_guard_lost", "outcome_unknown": False}
            assert first.get("/health/ready", headers=owner).status_code == 503
            assert first.get("/admin/").status_code == 503
            mcp = first.post(
                "/mcp",
                headers={
                    **auth(client["credential"]),
                    "Accept": "application/json, text/event-stream",
                },
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )
            assert mcp.status_code == 503
            history = first.portal.call(
                app.state.gateway.adapter.execute, account["id"], "history", "public-channel", {}
            )
            assert all(message["text"] != "must not be sent" for message in history["messages"])
        # The old worker must not silently reacquire the newly available lock.
        assert first.get("/health/ready", headers=owner).status_code == 503
        with app.state.sessions() as session:
            assert session.scalar(
                select(AuditEvent).where(
                    AuditEvent.event == "http_request", AuditEvent.error_code == "http_503"
                )
            )
    assert "gateway instance guard lost; restart required" in caplog.text
    assert "must not be sent" not in caplog.text


def test_sqlite_guard_rejects_preexisting_symlink_and_insecure_lock(settings, tmp_path):
    from pathlib import Path

    import pytest
    from fastapi.testclient import TestClient
    from sqlalchemy.engine import make_url
    from test_migrations import migrate

    from tg_control.api import create_app

    migrate(settings.database_url)
    database = Path(make_url(settings.database_url).database).resolve()
    lock = database.with_name(database.name + ".tg-control.lock")
    target = tmp_path / "synthetic-target"
    target.write_text("unchanged", encoding="utf-8")
    target.chmod(0o600)
    lock.symlink_to(target)
    with pytest.raises(RuntimeError, match="instance guard could not be acquired"):
        with TestClient(create_app(settings)):
            pass
    assert target.read_text(encoding="utf-8") == "unchanged"
    lock.unlink()
    lock.write_text("", encoding="utf-8")
    lock.chmod(0o644)
    with pytest.raises(RuntimeError, match="instance guard could not be acquired"):
        with TestClient(create_app(settings)):
            pass


def test_single_gateway_guard_releases_when_startup_fails(settings, monkeypatch):
    import pytest
    from fastapi.testclient import TestClient
    from test_migrations import migrate

    from tg_control.api import create_app

    migrate(settings.database_url)
    app = create_app(settings)

    async def fail_retention(*_args):
        raise RuntimeError("synthetic startup failure")

    monkeypatch.setattr(app.state.gateway, "configure_retention", fail_retention)
    with pytest.raises(RuntimeError, match="synthetic startup failure"):
        with TestClient(app):
            pass
    with TestClient(create_app(settings)) as restarted:
        assert restarted.get("/health/ready", headers=auth(settings.owner_token)).status_code == 200


def test_sqlite_instance_guard_sanitizes_path_failures(monkeypatch):
    from types import SimpleNamespace

    import pytest

    from tg_control.instance_guard import InstanceGuard

    class BadPath:
        def __init__(self, _database):
            pass

        def resolve(self):
            raise OSError("synthetic-private-path-detail")

    monkeypatch.setattr("tg_control.instance_guard.Path", BadPath)
    engine = SimpleNamespace(
        dialect=SimpleNamespace(name="sqlite"),
        url=SimpleNamespace(database="synthetic.sqlite", query={}),
    )
    with pytest.raises(RuntimeError, match="instance guard could not be acquired") as error:
        InstanceGuard(engine).acquire()
    assert "synthetic-private" not in str(error.value)
