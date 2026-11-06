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
    new = {"audit_retention_days": 30, "cache_ttl_seconds": 60, "media_retention_days": None}
    assert api.put("/v1/admin/settings", headers=owner, json=new).json() == new
    assert api.get("/v1/admin/settings", headers=owner).json() == new
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
