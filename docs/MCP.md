# MCP integration (synthetic only)

The MCP SDK exposes `tg_accounts` and `tg_execute`. The latter accepts a `batch` object with exactly the same schema, scopes, exclusions, one-time mutation key and results as `POST /v1/execute`. Message content, filenames and captions are **untrusted data**, not instructions. The tools do not provide owner management or direct adapter access. Errors are structured and do not echo rejected inputs.

For `tg_execute`, MCP sets `isError` when any batch item fails while retaining the per-item results. A successful-looking response with missing, malformed, contradictory or uncorrelated item outcomes is instead reported as `invalid_gateway_response`; a mutation's outcome is conservatively unknown. The stdio HTTP backend also treats non-2xx responses as errors even if their body claims success. These checks do not make a batch atomic or automatically retry it: inspect all items and reconcile unknown outcomes.

## Local stdio

Run the gateway first. Configure a compatible local MCP host to launch the installed `tg-control-mcp` executable (or `.venv/bin/python -m tg_control.mcp_stdio`) with:

- `TG_CLIENT_TOKEN_FILE`: path to that client's permission-restricted credential file, not the credential itself.
- `TG_API_URL`: API origin, default `http://127.0.0.1:8000`. Plain HTTP is allowed only for loopback. Remote origins require HTTPS. Redirects and ambient proxy settings are disabled.

For example, in a local MCP host's configuration, use its own syntax to set the command and those environment variables. Keep credential files outside Git and do not paste them into AI prompts. The stdio process sends its requests to the same HTTP API and cannot bypass its checks.

## Streamable HTTP

`/mcp` uses the official Python MCP SDK, stateless Streamable HTTP and per-request client Bearer authentication. Every request, including initialize and tool discovery, requires a currently valid client credential. Origin and Host are checked by the outer HTTP security boundary. TLS and restricted ingress are required for any approved remote exposure; the application is synthetic-only and no remote deployment is authorized here.

**Interoperability limitation (ADR-014):** this implementation accepts provisioned static Bearer credentials; it does not implement OAuth authorization-server discovery, dynamic client registration or protected-resource metadata, and those endpoints intentionally return `404` (asserted by `tests/test_mcp.py`). It can be used only by remote clients supporting a configured Bearer header. ChatGPT remote connector/OAuth compatibility has **not** been demonstrated and must not be assumed. The local stdio path works with MCP hosts that support subprocess servers. Tested with the MCP SDK's client over Streamable HTTP, in-memory streams and an actual local stdio subprocess; no live Telegram connection is involved.
