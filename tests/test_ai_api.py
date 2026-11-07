"""HTTP acceptance for the AI task contract (no approved utility)."""

from test_api import auth, create_resources

from tg_control.ai import FakeProvider, conformance_task


def test_ai_catalog_is_empty_and_owner_only_providers(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    assert api.get("/v1/ai/tasks", headers=auth(token)).json() == {"tasks": []}
    unknown = api.post(
        "/v1/ai/tasks/summarize/run",
        headers=auth(token),
        json={"input": {"text": "x"}, "provider": "synthetic-local"},
    )
    assert unknown.status_code == 404 and unknown.json()["error"] == "unknown_task"
    assert api.get("/v1/ai/tasks", headers=owner).status_code == 401
    assert api.get("/v1/admin/ai/providers", headers=auth(token)).status_code == 401
    assert api.get("/v1/admin/ai/providers", headers=owner).json() == {"providers": []}


def test_registered_conformance_task_enforces_compliance_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    api.app.state.ai.register_provider(FakeProvider())
    api.app.state.ai.register_task(conformance_task())
    assert api.get("/v1/ai/tasks", headers=auth(token)).json()["tasks"][0]["name"] == (
        "conformance.echo"
    )
    blocked = api.post(
        "/v1/ai/tasks/conformance.echo/run",
        headers=auth(token),
        json={
            "input": {"text": "synthetic", "chat_id": "public-channel"},
            "provider": "synthetic-local",
        },
    )
    assert blocked.status_code == 422 and blocked.json()["error"] == "compliance_blocked"
    allowed = api.post(
        "/v1/ai/tasks/conformance.echo/run",
        headers=auth(token),
        json={"input": {"text": "exact"}, "provider": "synthetic-local"},
    )
    assert allowed.status_code == 200, allowed.text
    body = allowed.json()
    assert body["output"] == {"text": "exact"}
    assert body["usage"]["attempts"] == 1
    missing_provider = api.post(
        "/v1/ai/tasks/conformance.echo/run",
        headers=auth(token),
        json={"input": {"text": "x"}, "provider": "remote-unknown"},
    )
    assert missing_provider.status_code == 404
    assert missing_provider.json()["error"] == "unknown_provider"


def test_ai_requires_client_credential(api, settings):
    owner, account, client = create_resources(api, settings)
    assert (
        api.post(
            "/v1/ai/tasks/conformance.echo/run",
            headers=owner,
            json={"input": {"text": "x"}, "provider": "synthetic-local"},
        ).status_code
        == 401
    )
