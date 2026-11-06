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
