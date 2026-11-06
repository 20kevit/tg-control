# CLI (synthetic only)

Install the Python package and start the gateway. Put a synthetic client credential into a permission-restricted file outside Git; pass its **path**, never the credential, on the command line:

```sh
tg-control --token-file /path/to/client-token accounts
tg-control --token-file /path/to/client-token execute --file batch.json
# Or pipe a JSON batch to stdin:
tg-control --token-file /path/to/client-token execute < batch.json
```

A mutation batch needs a new UUID `idempotency_key`; reuse is rejected. The JSON action schema is in `/openapi.json` and [API.md](API.md). Output is JSON; a request-level failure returns nonzero. Item-level failures can occur within HTTP 200: inspect every item. Credentials for newly created/rotated clients are returned only once to the **owner**; handle stdout as sensitive.

For owner operations the generic command calls a constrained `/v1/` path through the same API, e.g. `tg-control --token-file /path/to/owner-token request GET /v1/admin/clients`. `POST`, `PUT`, and `PATCH` accept `--file` (or `--file -` for stdin). The API, not the CLI, enforces identity and authorization. `TG_API_URL` or `--url` selects an origin; non-loopback HTTP, URLs containing credentials/paths, redirects, and arbitrary external request paths are rejected.
