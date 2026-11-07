"""HTTP acceptance for the owner-only plugin registry (first-party, synthetic)."""

from test_api import auth, create_resources


class Module:
    plugin_id = "firstparty.api"

    def run(self):
        return "ok"


def manifest(account, client, **overrides):
    body = {
        "plugin_id": "firstparty.api",
        "display_name": "API plugin",
        "publisher": "tg-control first party",
        "version": "1.0.0",
        "sdk_range": ">=1.0,<2.0",
        "entry_point": "tg_control_firstparty.api:run",
        "extension_points": ["worker"],
        "capabilities": ["read", "write"],
        "client_id": client["id"],
        "account_ids": [account["id"]],
    }
    body.update(overrides)
    return body


def test_plugin_registry_is_owner_only_and_starts_empty(api, settings):
    owner, account, client = create_resources(api, settings)
    token = client["credential"]
    assert api.get("/v1/admin/plugins", headers=owner).json() == {"plugins": []}
    assert api.get("/v1/admin/plugins", headers=auth(token)).status_code == 401


def test_install_grant_enable_and_quarantine_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    installed = api.post("/v1/admin/plugins", headers=owner, json=manifest(account, client))
    assert installed.status_code == 201, installed.text
    assert installed.json()["state"] == "installed"
    assert installed.json()["granted_capabilities"] == []
    assert api.post("/v1/admin/plugins/firstparty.api/enable", headers=owner).status_code == 409
    granted = api.post(
        "/v1/admin/plugins/firstparty.api/grant",
        headers=owner,
        json={"capabilities": ["read", "write"]},
    )
    assert granted.status_code == 200
    assert granted.json()["granted_capabilities"] == ["read", "write"]
    assert api.post(
        "/v1/admin/plugins/firstparty.api/grant",
        headers=owner,
        json={"capabilities": ["ai_task"]},
    ).status_code == 422
    assert api.post("/v1/admin/plugins/firstparty.api/enable", headers=owner).status_code == 409
    api.app.state.plugins.bind("firstparty.api", Module())
    enabled = api.post("/v1/admin/plugins/firstparty.api/enable", headers=owner)
    assert enabled.status_code == 200 and enabled.json()["state"] == "enabled"
    disabled = api.post("/v1/admin/plugins/firstparty.api/disable", headers=owner)
    assert disabled.json()["state"] == "disabled"
    unknown = api.get("/v1/admin/plugins/unknown.plugin", headers=owner)
    assert unknown.status_code == 404 and unknown.json()["error"] == "plugin_not_found"


def test_plugin_manifest_validation_over_http(api, settings):
    owner, account, client = create_resources(api, settings)
    inline_secret = api.post(
        "/v1/admin/plugins",
        headers=owner,
        json=manifest(account, client, config={"api_token": "literal-secret"}),
    )
    assert inline_secret.status_code == 422
    invalid_extension = api.post(
        "/v1/admin/plugins",
        headers=owner,
        json=manifest(account, client, extension_points=["event_consumer"]),
    )
    assert invalid_extension.status_code == 422
    admin_capability = api.post(
        "/v1/admin/plugins",
        headers=owner,
        json=manifest(account, client, capabilities=["admin"]),
    )
    assert admin_capability.status_code == 422
    valid = api.post(
        "/v1/admin/plugins",
        headers=owner,
        json=manifest(account, client, config={"api_key": "$secret:provider-key"}),
    )
    assert valid.status_code == 201
