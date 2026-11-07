"""HTTP acceptance for the shared command router and /save."""

import uuid

from test_api import auth, create_resources, execute

SAVE_POLICY = {
    "read": {
        "enabled": True,
        "allow_types": ["public_channel"],
        "allow_chats": ["saved-messages"],
    },
    "write": {"enabled": True, "allow_chats": ["saved-messages"]},
    "admin": {"enabled": False},
}


def set_save_policy(api, owner, account, client, policy=None):
    response = api.put(
        f"/v1/admin/clients/{client['id']}/accounts/{account['id']}/policy",
        headers=owner,
        json=policy or SAVE_POLICY,
    )
    assert response.status_code == 200, response.text


def saved_messages(api, token, account):
    response = execute(api, token, account, "history", "saved-messages")
    assert response.status_code == 200, response.text
    return response.json()["items"][0]["data"]["messages"]


def save(api, token, account, message_id=1, key=None, chat_id="public-channel"):
    return api.post(
        "/v1/commands/save",
        headers=auth(token),
        json={
            "account_id": account["id"],
            "chat_id": chat_id,
            "message_id": message_id,
            "idempotency_key": key or str(uuid.uuid4()),
        },
    )


def test_command_catalog_and_save_text_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    set_save_policy(api, owner, account, client)
    token = client["credential"]
    catalog = api.get("/v1/commands", headers=auth(token))
    assert catalog.status_code == 200
    assert [command["name"] for command in catalog.json()["commands"]] == ["save"]
    assert api.get("/v1/commands", headers=owner).status_code == 401
    result = save(api, token, account)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "success" and body["has_media"] is False
    assert body["destination_chat_id"] == "saved-messages"
    messages = saved_messages(api, token, account)
    assert any(message["text"].startswith("Synthetic fixture") for message in messages)


def test_save_media_over_http_preserves_filename(api, settings):
    owner, account, client = create_resources(api, settings)
    set_save_policy(api, owner, account, client)
    token = client["credential"]
    result = save(api, token, account, message_id=2)
    assert result.status_code == 200, result.text
    assert result.json()["has_media"] is True
    messages = saved_messages(api, token, account)
    latest = messages[0]
    assert latest["text"] == "Synthetic attachment"
    assert latest["media"]["filename"] == "sample.txt"


def test_save_denial_and_duplicate_invocation(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    denied = save(api, token, account)
    assert denied.status_code == 403 and denied.json()["error"] == "denied"
    set_save_policy(
        api,
        owner,
        account,
        client,
        {
            "read": {"enabled": True, "allow_types": ["public_channel"]},
            "write": {"enabled": True, "allow_chats": ["public-channel"]},
        },
    )
    destination = save(api, token, account)
    assert destination.status_code == 403
    assert destination.json()["error"] == "destination_denied"
    set_save_policy(api, owner, account, client)
    key = str(uuid.uuid4())
    assert save(api, token, account, key=key).status_code == 200
    duplicate = save(api, token, account, key=key)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"] == "duplicate_invocation"
    assert duplicate.json()["outcome_unknown"] is False


def test_save_missing_reply_and_invalid_input(api, settings):
    owner, account, client = create_resources(api, settings)
    set_save_policy(api, owner, account, client)
    token = client["credential"]
    missing = save(api, token, account, message_id=999)
    assert missing.status_code == 404 and missing.json()["error"] == "unresolved_reply"
    invalid = api.post(
        "/v1/commands/save",
        headers=auth(token),
        json={
            "account_id": account["id"],
            "chat_id": "public-channel",
            "message_id": 0,
            "idempotency_key": str(uuid.uuid4()),
        },
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"] == "invalid_request"
    assert invalid.json()["status"] == 422 and invalid.json()["retryable"] is False


def test_owner_delegated_save_applies_client_policy(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    body = {
        "client_id": client["id"],
        "account_id": account["id"],
        "chat_id": "public-channel",
        "message_id": 1,
        "idempotency_key": str(uuid.uuid4()),
    }
    # Without a policy the named client still cannot exceed its grants.
    assert api.post("/v1/admin/commands/save", headers=owner, json=body).status_code == 403
    assert api.post("/v1/admin/commands/save", headers=auth(token), json=body).status_code == 401
    set_save_policy(api, owner, account, client)
    allowed = api.post("/v1/admin/commands/save", headers=owner, json=body)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["status"] == "success"
    missing_client = api.post(
        "/v1/admin/commands/save",
        headers=owner,
        json={**body, "client_id": str(uuid.uuid4()), "idempotency_key": str(uuid.uuid4())},
    )
    assert missing_client.status_code == 404
    assert missing_client.json()["error"] == "client_not_found"


def test_save_audit_contains_no_content(api, settings):
    owner, account, client = create_resources(api, settings)
    set_save_policy(api, owner, account, client)
    token = client["credential"]
    assert save(api, token, account, message_id=2).status_code == 200
    from sqlalchemy import select

    from tg_control.db import AuditEvent

    with api.app.state.sessions() as session:
        rows = session.scalars(select(AuditEvent).where(AuditEvent.event == "command_save")).all()
    assert rows
    blob = " ".join(
        f"{row.event}|{row.decision}|{row.error_code}|{row.chat_id}|{row.account_id}"
        for row in rows
    )
    assert "Synthetic attachment" not in blob and "sample.txt" not in blob
