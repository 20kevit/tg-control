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


def test_postgres_single_gateway_guard_and_loss_are_fail_closed(tmp_path, caplog):
    import pytest
    from sqlalchemy import text
    from sqlalchemy.engine import make_url
    from test_migrations import migrate

    from tg_control.db import make_engine
    from tg_control.instance_guard import InstanceGuard

    server = pgserver.get_server(tmp_path / "single-instance-pgdata", cleanup_mode="delete")
    admin_engine = None
    try:
        url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        migrate(url)
        settings = Settings(
            url,
            "synthetic-owner-" + "y" * 32,
            Fernet.generate_key(),
            allowed_hosts=("testserver",),
            allowed_origins=("http://testserver",),
        )
        owner = {"Authorization": "Bearer " + settings.owner_token}
        admin_engine = make_engine(url)
        for loss in ("unlock", "terminate"):
            first_app = create_app(settings)
            with TestClient(first_app) as first:
                with pytest.raises(RuntimeError, match="another gateway is already using"):
                    with TestClient(create_app(settings)):
                        pass
                assert first.get("/health/ready", headers=owner).status_code == 200
                if loss == "unlock":

                    def release_lock():
                        first_app.state.instance_guard._connection.execute(
                            text("SELECT pg_advisory_unlock_all()")
                        )

                    first.portal.call(release_lock)
                else:
                    pid = first_app.state.instance_guard._backend_pid
                    with admin_engine.begin() as connection:
                        assert (
                            connection.execute(
                                text("SELECT pg_terminate_backend(:pid)"), {"pid": pid}
                            ).scalar_one()
                            is True
                        )
                with TestClient(create_app(settings)) as second:
                    assert second.get("/health/ready", headers=owner).status_code == 200
                    failed = first.get("/health/ready", headers=owner)
                    assert failed.status_code == 503
                    assert failed.json()["error"] == "instance_guard_lost"
                # Guard loss is latched even after the competing process exits.
                assert first.get("/health/ready", headers=owner).status_code == 503
            with TestClient(create_app(settings)) as restarted:
                assert restarted.get("/health/ready", headers=owner).status_code == 200
        # The advisory-lock verification also works as a non-superuser.
        with admin_engine.begin() as connection:
            connection.execute(text("CREATE ROLE synthetic_guard LOGIN"))
        restricted_url = (
            make_url(url).set(username="synthetic_guard").render_as_string(hide_password=False)
        )
        restricted_engine = make_engine(restricted_url)
        guard = InstanceGuard(restricted_engine)
        try:
            guard.acquire()
            assert guard.check()
        finally:
            guard.close()
            restricted_engine.dispose()
        assert "gateway instance guard lost; restart required" in caplog.text
        assert settings.owner_token not in caplog.text
    finally:
        if admin_engine is not None:
            admin_engine.dispose()
        server.cleanup()


def test_guard_sanitizes_driver_and_cleanup_failures(caplog):
    from types import SimpleNamespace

    import pytest

    from tg_control.instance_guard import InstanceGuard

    class FailedConnection:
        closed = False

        def execution_options(self, **_kwargs):
            return self

        def execute(self, *_args):
            raise RuntimeError("synthetic driver secret")

        def invalidate(self):
            raise RuntimeError("synthetic cleanup secret")

        def close(self):
            self.closed = True

    connection = FailedConnection()
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection)
    guard = InstanceGuard(engine)
    with pytest.raises(RuntimeError, match="instance guard could not be acquired") as error:
        guard.acquire()
    assert "synthetic" not in str(error.value)
    assert connection.closed
    assert "synthetic driver secret" not in caplog.text
    assert "synthetic cleanup secret" not in caplog.text
