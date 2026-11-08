import json
import sys
import uuid

import httpx
import pytest
from test_api import create_resources, set_policy

from tg_control import cli
from tg_control.http_client import GatewayHttpClient, validate_base_url


def test_cli_uses_api_and_enforces_authorization(api, settings, tmp_path, monkeypatch, capsys):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    tokenfile = tmp_path / "client-token"
    tokenfile.write_text(client["credential"])
    tokenfile.chmod(0o600)
    real_class = GatewayHttpClient
    monkeypatch.setattr(
        cli,
        "GatewayHttpClient",
        lambda url, token: real_class(url, token, transport=httpx.ASGITransport(app=api.app)),
    )
    monkeypatch.setattr(sys, "argv", ["tg-control", "--token-file", str(tokenfile), "accounts"])
    cli.main()
    assert json.loads(capsys.readouterr().out)["accounts"][0]["id"] == account["id"]
    batchfile = tmp_path / "batch.json"
    batchfile.write_text(
        json.dumps(
            {
                "actions": [
                    {
                        "operation": "send",
                        "account_id": account["id"],
                        "chat_id": "private-group",
                        "text": "must be denied",
                    }
                ],
                "idempotency_key": str(uuid.uuid4()),
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["tg-control", "--token-file", str(tokenfile), "execute", "--file", str(batchfile)],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "batch_precondition_failed" in output and client["credential"] not in output


def test_cli_capabilities_command_uses_shared_api(api, settings, tmp_path, monkeypatch, capsys):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    tokenfile = tmp_path / "client-token"
    tokenfile.write_text(client["credential"])
    tokenfile.chmod(0o600)
    real_class = GatewayHttpClient
    monkeypatch.setattr(
        cli,
        "GatewayHttpClient",
        lambda url, token: real_class(url, token, transport=httpx.ASGITransport(app=api.app)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "tg-control",
            "--token-file",
            str(tokenfile),
            "capabilities",
            "--account-id",
            account["id"],
        ],
    )
    cli.main()
    body = json.loads(capsys.readouterr().out)
    assert body["account_id"] == account["id"]
    assert body["live_adapter_enabled"] is False
    assert all(item["live_status"] == "blocked_compliance" for item in body["capabilities"])


def test_cli_rejects_insecure_urls_and_absolute_request_targets(
    api, settings, tmp_path, monkeypatch, capsys
):
    with pytest.raises(ValueError):
        validate_base_url("http://example.com")
    with pytest.raises(ValueError):
        validate_base_url("https://user:password@example.com")
    assert validate_base_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    tokenfile = tmp_path / "token"
    tokenfile.write_text(settings.owner_token)
    tokenfile.chmod(0o600)
    monkeypatch.setattr(
        sys,
        "argv",
        ["tg-control", "--token-file", str(tokenfile), "request", "GET", "//evil.invalid"],
    )
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2 and settings.owner_token not in capsys.readouterr().err


def test_cli_returns_nonzero_for_partial_batch_and_preserves_every_item(
    api, settings, tmp_path, monkeypatch, capsys
):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    tokenfile = tmp_path / "client-token"
    tokenfile.write_text(client["credential"])
    tokenfile.chmod(0o600)
    batchfile = tmp_path / "batch.json"
    batch = {
        "idempotency_key": str(uuid.uuid4()),
        "actions": [
            {
                "operation": "send",
                "account_id": account["id"],
                "chat_id": "public-channel",
                "text": "synthetic successful item",
            },
            {
                "operation": "delete",
                "account_id": account["id"],
                "chat_id": "public-channel",
                "message_id": 999_999,
            },
        ],
    }
    batchfile.write_text(json.dumps(batch))
    real_class = GatewayHttpClient
    monkeypatch.setattr(
        cli,
        "GatewayHttpClient",
        lambda url, token: real_class(url, token, transport=httpx.ASGITransport(app=api.app)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["tg-control", "--token-file", str(tokenfile), "execute", "--file", str(batchfile)],
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 1
    output = capsys.readouterr().out
    result = json.loads(output)
    assert [item["status"] for item in result["items"]] == ["success", "failure"]
    assert result["items"][1]["error"] == "not_found"
    assert client["credential"] not in output
    # A fully successful new batch is still a successful process invocation.
    batch["idempotency_key"] = str(uuid.uuid4())
    batch["actions"] = batch["actions"][:1]
    batchfile.write_text(json.dumps(batch))
    cli.main()
    assert json.loads(capsys.readouterr().out)["items"][0]["status"] == "success"


@pytest.mark.parametrize(
    "status,result,failed",
    [
        (
            200,
            {"items": [{"index": 0, "operation": "send", "status": "success", "data": {}}]},
            False,
        ),
        (
            200,
            {
                "items": [
                    {
                        "index": 0,
                        "operation": "send",
                        "status": "failure",
                        "error": "not_found",
                        "outcome_unknown": False,
                    }
                ]
            },
            True,
        ),
        (200, {"items": []}, True),
        (
            200,
            {"items": [{"index": True, "operation": "send", "status": "success", "data": {}}]},
            True,
        ),
        (
            200,
            {"items": [{"index": 0, "operation": "edit", "status": "success", "data": {}}]},
            True,
        ),
        (200, {"items": [{"index": 0, "operation": "send", "status": "unknown"}]}, True),
        (200, {"items": [None]}, True),
        (200, {"items": [{"index": 0, "operation": "send", "status": "success"}]}, True),
        (
            200,
            {
                "items": [
                    {
                        "index": 0,
                        "operation": "send",
                        "status": "success",
                        "data": {},
                        "error": "timeout",
                    }
                ]
            },
            True,
        ),
        (
            200,
            {"items": [{"index": 0, "operation": "send", "status": "failure", "error": "timeout"}]},
            True,
        ),
        (200, {"error": "synthetic_error"}, True),
        (
            302,
            {"items": [{"index": 0, "operation": "send", "status": "success", "data": {}}]},
            True,
        ),
        (
            503,
            {"items": [{"index": 0, "operation": "send", "status": "success", "data": {}}]},
            True,
        ),
    ],
)
def test_cli_requires_complete_correlated_successful_batch_results(status, result, failed):
    assert (
        cli._response_failed(
            status,
            result,
            method="POST",
            path="/v1/execute",
            body={"actions": [{"operation": "send"}]},
        )
        is failed
    )
