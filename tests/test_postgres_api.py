"""Full synthetic API path against a private ephemeral PostgreSQL instance."""

import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

import pgserver
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from tg_control.adapters.synthetic import SyntheticAdapter
from tg_control.api import create_app
from tg_control.config import Settings


def test_postgres_api_round_trip(tmp_path):
    server = pgserver.get_server(tmp_path / "pgdata-api", cleanup_mode="delete")
    try:
        url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        env = os.environ.copy()
        env["TG_DATABASE_URL"] = url
        env.pop("TG_DATABASE_URL_FILE", None)
        root = Path(__file__).resolve().parents[1]
        subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=root,
            env=env,
            check=True,
            capture_output=True,
        )
        settings = Settings(
            url,
            "synthetic-owner-" + "x" * 32,
            Fernet.generate_key(),
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
        )
        adapter = SyntheticAdapter()
        with TestClient(create_app(settings, adapter)) as api:
            owner = {"Authorization": "Bearer " + settings.owner_token}
            account = api.post(
                "/v1/admin/accounts",
                headers=owner,
                json={"alias": "pg synthetic", "phone_number": "+12025550101"},
            ).json()
            client = api.post("/v1/admin/clients", headers=owner, json={"name": "pg client"}).json()
            policy = {
                "read": {"enabled": True, "allow_types": ["public_channel"]},
                "write": {"enabled": True, "allow_chats": ["public-channel"]},
            }
            route = f"/v1/admin/clients/{client['id']}/accounts/{account['id']}/policy"
            assert api.put(route, headers=owner, json=policy).status_code == 200
            headers = {"Authorization": "Bearer " + client["credential"]}
            body = {
                "idempotency_key": str(uuid4()),
                "actions": [
                    {
                        "operation": "send",
                        "account_id": account["id"],
                        "chat_id": "public-channel",
                        "text": "pg synthetic",
                    }
                ],
            }
            assert (
                api.post("/v1/execute", headers=headers, json=body).json()["items"][0]["status"]
                == "success"
            )
            assert api.post("/v1/execute", headers=headers, json=body).status_code == 409
            assert (
                api.post(f"/v1/admin/clients/{client['id']}/revoke", headers=owner).status_code
                == 200
            )
            assert api.get("/v1/accounts", headers=headers).status_code == 401
            adapter.set_state(account["id"], "reauth_required")
            api.portal.call(api.app.state.gateway.maintain_sessions)
            events = api.get("/v1/admin/audit?event=session_transition", headers=owner).json()[
                "events"
            ]
            assert len(events) == 1 and events[0]["decision"] == "reauth_required"
        restarted_adapter = SyntheticAdapter()
        restarted_adapter.set_state(account["id"], "reauth_required")
        with TestClient(create_app(settings, restarted_adapter)) as api:
            api.portal.call(api.app.state.gateway.maintain_sessions)
            events = api.get("/v1/admin/audit?event=session_transition", headers=owner).json()[
                "events"
            ]
            assert len(events) == 1 and events[0]["account_id"] == account["id"]
    finally:
        server.cleanup()
