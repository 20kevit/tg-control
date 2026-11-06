"""Exercise the actual stdio executable against a loopback-only synthetic API."""

import asyncio
import json
import os
import socket
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from tg_control.api import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.asyncio
async def test_stdio_executable_uses_gateway_and_revocation(settings, tmp_path):
    env = os.environ.copy()
    env["TG_DATABASE_URL"] = settings.database_url
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(128)
    origin = f"http://127.0.0.1:{sock.getsockname()[1]}"
    settings = replace(settings, allowed_hosts=("127.0.0.1",), allowed_origins=(origin,))
    ready = asyncio.Event()

    class TestServer(uvicorn.Server):
        async def startup(self, sockets=None):
            await super().startup(sockets=sockets)
            ready.set()

    server = TestServer(
        uvicorn.Config(
            create_app(settings),
            log_level="critical",
            access_log=False,
            lifespan="on",
            log_config=None,
        )
    )
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        await asyncio.wait_for(ready.wait(), timeout=15)
        owner_file = tmp_path / "owner-token"
        owner_file.write_text(settings.owner_token)
        owner_file.chmod(0o600)
        health_env = {
            **os.environ,
            "TG_OWNER_TOKEN_FILE": str(owner_file),
            "TG_HEALTH_PORT": str(sock.getsockname()[1]),
            "HTTP_PROXY": "http://127.0.0.1:1",
        }

        async def healthcheck():
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tg_control.healthcheck",
                env=health_env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
            assert settings.owner_token.encode() not in stdout + stderr
            return process.returncode, stderr

        code, _stderr = await healthcheck()
        assert code == 0
        owner_file.write_text("wrong-synthetic-token-" + "x" * 32)
        code, stderr = await healthcheck()
        assert code == 1 and stderr.strip() == b"health check failed"
        owner_file.write_text(settings.owner_token)
        async with httpx.AsyncClient(base_url=origin, trust_env=False) as api:
            owner = {"Authorization": "Bearer " + settings.owner_token}
            account = (
                await api.post(
                    "/v1/admin/accounts",
                    headers=owner,
                    json={"alias": "stdio account", "phone_number": "+12025550102"},
                )
            ).json()
            client = (
                await api.post("/v1/admin/clients", headers=owner, json={"name": "stdio client"})
            ).json()
            policy = {"read": {"enabled": True, "allow_types": ["public_channel"]}}
            endpoint = f"/v1/admin/clients/{client['id']}/accounts/{account['id']}/policy"
            assert (await api.put(endpoint, headers=owner, json=policy)).status_code == 200
            token_file = tmp_path / "stdio-token"
            token_file.write_text(client["credential"])
            token_file.chmod(0o600)
            child_env = {
                **os.environ,
                "TG_CLIENT_TOKEN_FILE": str(token_file),
                "TG_API_URL": origin,
            }

            async def cli_accounts():
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "tg_control.cli",
                    "--url",
                    origin,
                    "--token-file",
                    str(token_file),
                    "accounts",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=15)
                assert client["credential"].encode() not in stdout + stderr
                return process.returncode, json.loads(stdout)

            code, data = await cli_accounts()
            assert code == 0 and data["accounts"][0]["id"] == account["id"]
            parameters = StdioServerParameters(
                command=sys.executable, args=["-m", "tg_control.mcp_stdio"], env=child_env, cwd=ROOT
            )
            error_output = (tmp_path / "mcp-stderr").open("w+")
            async with stdio_client(parameters, errlog=error_output) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool("tg_accounts", {})
                    assert result.structured_content["accounts"][0]["id"] == account["id"]
                    await api.post(f"/v1/admin/clients/{client['id']}/revoke", headers=owner)
                    result = await session.call_tool("tg_accounts", {})
                    assert (
                        result.is_error
                        and result.structured_content["error"] == "invalid_credential"
                    )
            code, data = await cli_accounts()
            assert code == 1 and data["error"] == "invalid_credential"
            error_output.seek(0)
            assert client["credential"] not in error_output.read()
            error_output.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)
        sock.close()
