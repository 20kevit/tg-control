import json
import uuid

from test_api import auth, create_resources, set_policy


def rpc(api, token, method, params=None, request_id=1, origin=None):
    headers = {
        **auth(token),
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if origin:
        headers["Origin"] = origin
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return api.post("/mcp", headers=headers, json=body)


def test_mcp_streamable_http_auth_tools_scope_and_revocation(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    token = client["credential"]
    assert api.post("/mcp", json={}).status_code == 401
    assert api.get("/mcp/").status_code == 401
    assert rpc(api, settings.owner_token, "tools/list").status_code == 401
    assert rpc(api, token, "tools/list", origin="https://evil.invalid").status_code == 403
    assert rpc(api, token, "tools/list", origin="http://testserver").status_code == 200
    assert (
        api.post("/mcp", headers={**auth(token), "Host": "evil.invalid"}, json={}).status_code
        == 400
    )
    init = rpc(
        api,
        token,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "synthetic-test", "version": "1"},
        },
    )
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "tg-control"
    listed = rpc(api, token, "tools/list").json()
    assert {tool["name"] for tool in listed["result"]["tools"]} == {"tg_accounts", "tg_execute"}
    accounts = rpc(api, token, "tools/call", {"name": "tg_accounts", "arguments": {}})
    assert accounts.status_code == 200, accounts.text
    assert accounts.json()["result"]["structuredContent"]["accounts"][0]["id"] == account["id"]
    denied = rpc(
        api,
        token,
        "tools/call",
        {
            "name": "tg_execute",
            "arguments": {
                "batch": {
                    "actions": [
                        {
                            "account_id": account["id"],
                            "operation": "history",
                            "chat_id": "private-direct",
                        }
                    ]
                }
            },
        },
    ).json()["result"]
    assert (
        denied["isError"] is True
        and denied["structuredContent"]["error"] == "batch_precondition_failed"
    )
    good = rpc(
        api,
        token,
        "tools/call",
        {
            "name": "tg_execute",
            "arguments": {
                "batch": {
                    "actions": [
                        {
                            "account_id": account["id"],
                            "operation": "send",
                            "chat_id": "public-channel",
                            "text": "synthetic mcp",
                        }
                    ],
                    "idempotency_key": str(uuid.uuid4()),
                }
            },
        },
    ).json()["result"]
    assert good["structuredContent"]["items"][0]["status"] == "success"
    rotated = api.post(f"/v1/admin/clients/{client['id']}/rotate", headers=owner).json()[
        "credential"
    ]
    assert rpc(api, token, "tools/list").status_code == 401
    assert rpc(api, rotated, "tools/list").status_code == 200
    api.post(f"/v1/admin/clients/{client['id']}/revoke", headers=owner)
    assert rpc(api, rotated, "tools/list").status_code == 401
    events = api.get("/v1/admin/audit", headers=owner).json()["events"]
    assert any(e["event"] == "boundary_rejection" and e["error_code"] == "http_403" for e in events)
    assert any(e["event"] == "boundary_rejection" and e["error_code"] == "http_400" for e in events)
    assert token not in json.dumps(events) and rotated not in json.dumps(events)


def test_mcp_invalid_input_never_echoes_content(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    marker = "SYNTHETIC-SENSITIVE-MARKER"
    response = rpc(
        api,
        client["credential"],
        "tools/call",
        {
            "name": "tg_execute",
            "arguments": {
                "batch": {
                    "actions": [
                        {
                            "account_id": account["id"],
                            "operation": "send",
                            "chat_id": "public-channel",
                            "text": marker,
                        }
                    ]
                }
            },
        },
    )
    assert response.status_code == 200
    assert marker not in response.text
    result = response.json()["result"]
    assert result["isError"] and result["structuredContent"]["error"] == "invalid_request"
    assert marker not in json.dumps(api.get("/v1/admin/audit", headers=owner).json())


async def test_mcp_sdk_stdio_backend_over_shared_api(api, settings):
    import httpx
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    from tg_control.http_client import GatewayHttpClient
    from tg_control.mcp_server import HttpBackend, create_mcp_server

    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    transport = httpx.ASGITransport(app=api.app)
    backend = HttpBackend(
        GatewayHttpClient("http://localhost", client["credential"], transport=transport)
    )
    server = create_mcp_server(backend)
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            assert init.server_info.name == "tg-control"
            tools = await session.list_tools()
            assert {tool.name for tool in tools.tools} == {"tg_accounts", "tg_execute"}
            accounts = await session.call_tool("tg_accounts", {})
            assert accounts.structured_content["accounts"][0]["id"] == account["id"]
            denied = await session.call_tool(
                "tg_execute",
                {
                    "batch": {
                        "actions": [
                            {
                                "account_id": account["id"],
                                "operation": "history",
                                "chat_id": "private-direct",
                            }
                        ]
                    }
                },
            )
            assert (
                denied.is_error
                and denied.structured_content["error"] == "batch_precondition_failed"
            )


async def test_official_sdk_streamable_http_client(api, settings):
    import httpx2
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    transport = httpx2.ASGITransport(app=api.app)
    async with httpx2.AsyncClient(transport=transport, headers=auth(client["credential"])) as http:
        async with streamable_http_client("http://testserver/mcp", http_client=http) as (
            read,
            write,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool("tg_accounts", {})
                assert result.structured_content["accounts"][0]["id"] == account["id"]


def test_mcp_partial_batch_is_error_but_reports_all_outcomes(api, settings):
    owner, account, client = create_resources(api, settings)
    set_policy(api, owner, account, client)
    batch = {
        "idempotency_key": str(uuid.uuid4()),
        "actions": [
            {
                "account_id": account["id"],
                "operation": "send",
                "chat_id": "public-channel",
                "text": "synthetic mcp successful item",
            },
            {
                "account_id": account["id"],
                "operation": "delete",
                "chat_id": "public-channel",
                "message_id": 999_999,
            },
        ],
    }
    result = rpc(
        api,
        client["credential"],
        "tools/call",
        {"name": "tg_execute", "arguments": {"batch": batch}},
    ).json()["result"]
    assert result["isError"] is True
    assert [item["status"] for item in result["structuredContent"]["items"]] == [
        "success",
        "failure",
    ]
    assert result["structuredContent"]["items"][1]["error"] == "not_found"
    assert client["credential"] not in json.dumps(result)


async def test_mcp_does_not_claim_success_for_malformed_batch_results():
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    from tg_control.mcp_server import create_mcp_server

    account = str(uuid.uuid4())
    batch = {
        "idempotency_key": str(uuid.uuid4()),
        "actions": [{"operation": "send", "account_id": account, "chat_id": "chat", "text": "x"}],
    }
    malformed = [
        {"items": []},
        {"items": [{"index": 0, "operation": "edit", "status": "success", "data": {}}]},
        {"items": [{"index": True, "operation": "send", "status": "success", "data": {}}]},
        {"items": [{"index": 0, "operation": "send", "status": "unknown"}]},
        {"items": [None], "private": "synthetic-upstream-detail"},
        None,
    ]

    class MalformedBackend:
        async def accounts(self, _context):
            return {"accounts": []}

        async def execute(self, _context, _batch):
            return malformed.pop(0)

    server = create_mcp_server(MalformedBackend())
    async with InMemoryTransport(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            for _ in range(6):
                result = await session.call_tool("tg_execute", {"batch": batch})
                assert result.is_error
                assert result.structured_content == {
                    "error": "invalid_gateway_response",
                    "outcome_unknown": True,
                }
                assert "synthetic-upstream-detail" not in str(result)


async def test_stdio_mcp_does_not_ignore_http_failure_status():
    import httpx
    from mcp.client._memory import InMemoryTransport
    from mcp.client.session import ClientSession

    from tg_control.http_client import GatewayHttpClient
    from tg_control.mcp_server import HttpBackend, create_mcp_server

    def unavailable(request):
        if request.url.path == "/v1/accounts":
            return httpx.Response(503, json={"accounts": []})
        return httpx.Response(
            503,
            json={"items": [{"index": 0, "operation": "send", "status": "success", "data": {}}]},
        )

    backend = HttpBackend(
        GatewayHttpClient(
            "http://localhost", "synthetic-token", transport=httpx.MockTransport(unavailable)
        )
    )
    batch = {
        "idempotency_key": str(uuid.uuid4()),
        "actions": [
            {"operation": "send", "account_id": str(uuid.uuid4()), "chat_id": "chat", "text": "x"}
        ],
    }
    async with InMemoryTransport(create_mcp_server(backend)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            accounts = await session.call_tool("tg_accounts", {})
            assert accounts.is_error
            assert accounts.structured_content == {
                "error": "gateway_http_error",
                "outcome_unknown": False,
            }
            execute = await session.call_tool("tg_execute", {"batch": batch})
            assert execute.is_error
            assert execute.structured_content == {
                "error": "gateway_http_error",
                "outcome_unknown": True,
            }
