#!/usr/bin/env bash
# Disposable single-instance container smoke test.
#
# Requires a working Docker daemon (CI runner or an owner-approved local host).
# It never publishes an image, never exposes a port beyond 127.0.0.1 and removes
# the stack on exit. All data is synthetic.
#
# Usage: scripts/container_smoke.sh
set -euo pipefail
# A command that fails under `set -e` skips every explicit message, so report the
# line and command as an annotation; a hosted run is then diagnosable at once.
trap 'code=$?; printf "::error title=container-smoke::line %s failed (exit %s): %s\n" \
  "$LINENO" "$code" "$BASH_COMMAND"' ERR
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

log() { printf '\n== %s\n' "$1"; }
fail() {
  printf 'FAIL: %s\n' "$1" >&2
  # A work-flow annotation keeps a hosted failure diagnosable without log access.
  printf '::error title=container-smoke::%s\n' "$(printf '%s' "$1" | tr '\n\r' '  ')"
  exit 1
}

command -v docker >/dev/null 2>&1 || fail "docker CLI is unavailable"
docker info >/dev/null 2>&1 || fail "docker daemon is unavailable"
docker compose version >/dev/null 2>&1 || fail "docker compose v2 is unavailable"

cleanup() {
  docker compose down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

log "build image"
docker compose build gateway migrate

log "validate compose configuration"
docker compose config -q

log "prepare synthetic secrets and persistent state directories"
if [ -e secrets/owner_token ]; then
  fail "secrets/ already contains state; refusing to overwrite an operator's files"
fi
"${PYTHON:-python3}" scripts/init_synthetic_secrets.py --directory ./secrets
# Read the owner token while the invoking user can still read it: the secrets are
# made 0400 and owned by the container UID below.
TOKEN="$(cat secrets/owner_token)"
[ -n "$TOKEN" ] || fail "owner token file is empty"
mkdir -p state/postgres state/synthetic
chmod 700 state state/postgres state/synthetic
# The image runs as an unprivileged UID with no capabilities; the bind mounts
# must be owned by it. The runner needs sudo for this, an interactive host uses
# the documented chown commands.
UID_TARGET="${TG_UID:-10001}"
GID_TARGET="${TG_GID:-10001}"
# Compose file secrets are bind mounts: the file keeps its host ownership and
# mode inside the container, so a 0600 root-owned secret is unreadable by the
# unprivileged container user. Own them by the target UID and make them read-only.
# Tighten permissions while the invoking user still owns the files; after the
# chown only the target UID can change them (sudo chmod would be needed).
chmod 700 secrets
chmod 400 secrets/*
if [ "$(stat -c %u state/postgres)" != "$UID_TARGET" ] ||
   [ "$(stat -c %u secrets/owner_token)" != "$UID_TARGET" ]; then
  if command -v sudo >/dev/null 2>&1; then
    sudo chown -R "$UID_TARGET:$GID_TARGET" state secrets
  else
    chown -R "$UID_TARGET:$GID_TARGET" state secrets 2>/dev/null || \
      fail "state and secrets must be owned by $UID_TARGET:$GID_TARGET (chown them first)"
  fi
fi

log "start database"
docker compose up -d --wait --wait-timeout 180 db

log "read-only preflight must refuse to pass before migrations are applied"
if docker compose run --rm --no-deps --entrypoint python gateway \
    -m tg_control.preflight; then
  fail "preflight passed on an unmigrated database; the schema gate is not working"
fi

log "migrate explicitly (one-shot service)"
docker compose run --rm migrate

log "read-only deployment preflight after migration"
docker compose run --rm --no-deps --entrypoint python gateway -m tg_control.preflight

log "start gateway"
docker compose up -d --wait --wait-timeout 240 gateway

health() {
  local path="$1"
  curl -fsS --max-time 5 -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000$path"
}
health /health/live >/dev/null || fail "liveness failed"
health /health/ready >/dev/null || fail "readiness failed"
health /health/dependencies >/dev/null || fail "dependency health failed"
if curl -fsS --max-time 5 "http://127.0.0.1:8000/health/ready" >/dev/null 2>&1; then
  fail "readiness must require the owner credential"
fi
log "health endpoints OK"

log "non-root execution checks"
CONTAINER_UID="$(docker compose exec -T gateway id -u)"
[ "$CONTAINER_UID" = "$UID_TARGET" ] || fail "gateway is not running as $UID_TARGET"
if docker compose exec -T gateway sh -c 'id -u' 2>/dev/null | grep -qx 0; then
  fail "gateway must not run as root"
fi

log "synthetic API smoke"
python3 - "$TOKEN" <<'PY'
import json
import sys
import urllib.request

token = sys.argv[1]
base = "http://127.0.0.1:8000"


def call(method, path, payload=None, credential=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {credential or token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read() or b"{}")


account = call("POST", "/v1/admin/accounts", {"alias": "container smoke", "phone_number": "+12025550142"})
client = call("POST", "/v1/admin/clients", {"name": "container smoke client"})
call(
    "PUT",
    f"/v1/admin/clients/{client['id']}/accounts/{account['id']}/policy",
    {
        "read": {"enabled": True, "allow_types": ["public_channel"]},
        "write": {"enabled": True, "allow_chats": ["public-channel"]},
        "admin": {"enabled": False},
    },
)
batch = call(
    "POST",
    "/v1/execute",
    {
        "actions": [
            {
                "account_id": account["id"],
                "operation": "send",
                "chat_id": "public-channel",
                "text": "synthetic container smoke",
            }
        ],
        "idempotency_key": __import__("uuid").str(__import__("uuid").uuid4()),
    },
    credential=client["credential"],
)
assert batch["items"][0]["status"] == "success", batch
accounts = call("GET", "/v1/accounts", credential=client["credential"])
assert accounts["accounts"][0]["id"] == account["id"]
print("api smoke ok", account["id"])
PY

log "state persistence across container recreation"
docker compose restart gateway
for _ in $(seq 1 60); do
  if health /health/ready >/dev/null 2>&1; then break; fi
  sleep 2
done
health /health/ready >/dev/null || fail "gateway did not become ready after restart"
docker compose down --remove-orphans
docker compose up -d --wait --wait-timeout 240 gateway
health /health/ready >/dev/null || fail "gateway did not become ready after recreation"
mkdir -p /tmp/container-smoke
health /health/live >/dev/null || fail "liveness failed after recreation"
log "persistence OK"

log "graceful shutdown"
docker compose stop gateway
SHUTDOWN_LOGS="$(docker compose logs --no-color gateway)"
printf '%s' "$SHUTDOWN_LOGS" | grep -q "Application shutdown complete" \
  || fail "graceful application shutdown was not observed in logs"

log "restart after graceful stop"
docker compose up -d --wait --wait-timeout 240 gateway
health /health/ready >/dev/null || fail "gateway did not become ready after graceful stop"

log "secret leakage scan of container logs"
FULL_LOGS="$(docker compose logs --no-color)"
for secret in secrets/owner_token secrets/encryption_key secrets/postgres_password secrets/database_url; do
  value="$(cat "$secret")"
  if printf '%s' "$FULL_LOGS" | grep -qF "$value"; then
    fail "secret from $secret appears in container logs"
  fi
done
log "log scan OK"

log "every health route fails closed when the database is unavailable"
# Audit writes are fail-closed, so liveness is 503 too: the container becomes
# unhealthy during a database outage rather than reporting a healthy process.
docker compose stop db
for path in /health/live /health/ready /health/dependencies; do
  STATUS="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 \
    -H "Authorization: Bearer $TOKEN" "http://127.0.0.1:8000$path" || true)"
  [ "$STATUS" = "503" ] || fail "expected 503 from $path without a database, got $STATUS"
done
docker compose start db
for _ in $(seq 1 60); do
  if health /health/ready >/dev/null 2>&1; then break; fi
  sleep 2
done
health /health/ready >/dev/null || fail "gateway did not recover readiness after the database returned"
log "container smoke test completed successfully"
