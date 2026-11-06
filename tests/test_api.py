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
    assert api.get("/health/live", headers={"Origin": "https://evil.invalid"}).status_code == 403
    assert api.get("/health/live", headers={"Host": "evil.invalid"}).status_code == 400
    ok = api.get("/health/live", headers={"Origin": "http://testserver"})
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
    assert api.get("/health/ready").json()["mode"] == "synthetic"
    assert (
        api.get(f"/v1/admin/accounts/{account['id']}/status", headers=owner).json()["state"]
        == "ready"
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
