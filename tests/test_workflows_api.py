"""HTTP acceptance for owner-configured workflows (synthetic only)."""

import uuid

from test_api import auth, create_resources, execute, set_policy


def definition(account, client, **overrides):
    body = {
        "name": "synthetic workflow",
        "trigger": "manual",
        "client_id": client["id"],
        "actions": [
            {
                "kind": "send",
                "account_id": account["id"],
                "chat_id": "public-channel",
                "text": "workflow over http",
            }
        ],
    }
    body.update(overrides)
    return body


def test_workflow_owner_lifecycle_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    assert api.get("/v1/admin/workflows", headers=auth(token)).status_code == 401
    created = api.post("/v1/admin/workflows", headers=owner, json=definition(account, client))
    assert created.status_code == 201, created.text
    workflow_id = created.json()["workflow_id"]
    assert created.json()["version"] == 1
    listed = api.get("/v1/admin/workflows", headers=owner).json()["workflows"]
    assert [item["workflow_id"] for item in listed] == [workflow_id]

    dry = api.post(
        f"/v1/admin/workflows/{workflow_id}/run", headers=owner, json={"dry_run": True}
    )
    assert dry.status_code == 200
    assert dry.json()["steps"][0]["status"] == "would_execute"
    assert dry.json()["state"] == "succeeded"

    run = api.post(
        f"/v1/admin/workflows/{workflow_id}/run",
        headers=owner,
        json={"parameters": {}, "dry_run": False},
    )
    assert run.status_code == 200 and run.json()["state"] == "succeeded"
    history = execute(api, token, account, "history", "public-channel").json()
    texts = [message["text"] for message in history["items"][0]["data"]["messages"]]
    assert "workflow over http" in texts

    runs = api.get(f"/v1/admin/workflows/{workflow_id}/runs", headers=owner)
    assert len(runs.json()["runs"]) == 2
    run_id = run.json()["run_id"]
    assert api.get(f"/v1/admin/workflow-runs/{run_id}", headers=owner).status_code == 200
    assert api.get(f"/v1/admin/workflow-runs/{run_id}", headers=auth(token)).status_code == 401


def test_workflow_denied_when_client_policy_revoked(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    workflow_id = api.post(
        "/v1/admin/workflows", headers=owner, json=definition(account, client)
    ).json()["workflow_id"]
    set_policy(api, owner, account, client, {"read": {"enabled": True, "allow_types": []}})
    run = api.post(f"/v1/admin/workflows/{workflow_id}/run", headers=owner, json={})
    assert run.status_code == 200
    assert run.json()["state"] == "partial_failure"
    assert run.json()["steps"][0]["status"] == "failure"
    history = execute(api, client["credential"], account, "history", "public-channel")
    assert history.status_code == 403


def test_workflow_validation_and_disable(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    invalid = api.post(
        "/v1/admin/workflows",
        headers=owner,
        json=definition(account, client, trigger="event"),
    )
    assert invalid.status_code == 422
    unknown_client = api.post(
        "/v1/admin/workflows",
        headers=owner,
        json=definition(account, {"id": str(uuid.uuid4())}),
    )
    assert unknown_client.status_code == 422
    assert unknown_client.json()["error"] == "unknown_client"
    workflow_id = api.post(
        "/v1/admin/workflows", headers=owner, json=definition(account, client)
    ).json()["workflow_id"]
    assert api.post(f"/v1/admin/workflows/{workflow_id}/disable", headers=owner).status_code == 200
    blocked = api.post(f"/v1/admin/workflows/{workflow_id}/run", headers=owner, json={})
    assert blocked.status_code == 409 and blocked.json()["error"] == "workflow_disabled"
    assert api.post(f"/v1/admin/workflows/{workflow_id}/enable", headers=owner).status_code == 200
    updated = api.put(
        f"/v1/admin/workflows/{workflow_id}",
        headers=owner,
        json=definition(account, client),
    )
    assert updated.status_code == 200 and updated.json()["version"] == 2
    missing = api.get(f"/v1/admin/workflows/{uuid.uuid4()}", headers=owner)
    assert missing.status_code == 404 and missing.json()["error"] == "workflow_not_found"
