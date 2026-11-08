# CLI (synthetic only)

Install the Python package and start the gateway. Put a synthetic client credential into a permission-restricted file outside Git; pass its **path**, never the credential, on the command line:

```sh
tg-control --token-file /path/to/client-token accounts
tg-control --token-file /path/to/client-token capabilities --account-id <account-uuid>
tg-control --token-file /path/to/client-token execute --file batch.json
# Or pipe a JSON batch to stdin:
tg-control --token-file /path/to/client-token execute < batch.json
```

The `capabilities` command calls the shared account endpoint and returns only operation rows allowed by the client's current permissions and accessible-chat scopes; missing chat metadata hides chat-scoped rows. It returns no chat IDs or content, does not reconnect, and is descriptive rather than an authorization grant or evidence of live Telegram support. The owner-only HTTP/Admin view retains the full catalogue. See [FEATURE_CAPABILITIES.md](FEATURE_CAPABILITIES.md), the [147-entry inventory](contracts/capability_catalog.json), and [FEATURE_TRACEABILITY.md](FEATURE_TRACEABILITY.md). A mutation batch needs a new UUID `idempotency_key`; reuse is rejected. The JSON action schema is in `/openapi.json` and [API.md](API.md). Output is JSON. For `execute` (also generic `request POST /v1/execute`), exit code 0 means the HTTP response succeeded and every requested item has a complete, correctly correlated `success` result. Exit 1 means a request, item or response-validation failure, including an HTTP-200 partial batch; exit 2 indicates invalid local configuration/input. The complete response remains on stdout: inspect every item and its `outcome_unknown` flag. A failure does **not** roll back earlier successes; reconcile them and never blindly repeat a batch. Credentials for newly created/rotated clients are returned only once to the **owner**; handle stdout as sensitive.

For owner operations the generic command calls a constrained `/v1/` path through the same API, e.g. `tg-control --token-file /path/to/owner-token request GET /v1/admin/clients`. `POST`, `PUT`, and `PATCH` accept `--file` (or `--file -` for stdin). The API, not the CLI, enforces identity and authorization. `TG_API_URL` or `--url` selects an origin; non-loopback HTTP, URLs containing credentials/paths, redirects, and arbitrary external request paths are rejected.
