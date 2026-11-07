"""Run actual migrations against SQLite and an isolated local PostgreSQL socket."""

import os
import subprocess
import sys
from pathlib import Path

import pgserver
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select

from tg_control.db import make_engine

ROOT = Path(__file__).resolve().parents[1]


def migration(url, command, revision):
    env = os.environ.copy()
    env["TG_DATABASE_URL"] = url
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def upgrade(url, revision):
    migration(url, "upgrade", revision)


def migrate(url):
    upgrade(url, "head")
    engine = make_engine(url)
    assert {
        "accounts",
        "clients",
        "credentials",
        "grants",
        "settings",
        "execution_keys",
        "audit_events",
        "alembic_version",
    } <= set(inspect(engine).get_table_names())
    assert "ix_audit_event_id" in {
        index["name"] for index in inspect(engine).get_indexes("audit_events")
    }
    engine.dispose()


def test_sqlite_migration(tmp_path):
    migrate(f"sqlite:///{tmp_path / 'migration.sqlite'}")


def test_postgres_migration_and_storage(tmp_path):
    # pgserver uses an isolated temporary Unix socket with no TCP listener.
    server = pgserver.get_server(tmp_path / "pgdata", cleanup_mode="delete")
    try:
        url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        from tg_control.auth import authenticate, rotate_credential
        from tg_control.db import AuditEvent, Client, make_sessions

        upgrade(url, "0003")
        engine = make_engine(url)
        sessions = make_sessions(engine)
        with sessions() as session:
            session.add(
                AuditEvent(
                    request_id="synthetic-request",
                    actor_kind="system",
                    event="synthetic",
                    decision="success",
                )
            )
            session.commit()
        engine.dispose()
        migrate(url)
        engine = make_engine(url)
        sessions = make_sessions(engine)
        with sessions() as session:
            assert session.scalar(select(AuditEvent).where(AuditEvent.event == "synthetic"))
            client = Client(name="synthetic postgres client")
            session.add(client)
            session.flush()
            token = rotate_credential(session, client.id)
            session.commit()
            assert authenticate(session, token) is not None
        engine.dispose()
    finally:
        server.cleanup()


def test_incremental_index_migration_preserves_audit_and_old_schema_fails_startup(settings):
    from tg_control.api import create_app
    from tg_control.db import AuditEvent, make_sessions

    upgrade(settings.database_url, "0003")
    engine = make_engine(settings.database_url)
    with make_sessions(engine)() as session:
        session.add(
            AuditEvent(
                request_id="synthetic-request",
                actor_kind="system",
                event="synthetic",
                decision="success",
            )
        )
        session.commit()
    engine.dispose()
    with pytest.raises(RuntimeError, match="does not match this build"):
        with TestClient(create_app(settings)):
            pass
    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    with make_sessions(engine)() as session:
        assert session.scalar(select(AuditEvent).where(AuditEvent.event == "synthetic"))
    engine.dispose()
    migration(settings.database_url, "downgrade", "0003")
    engine = make_engine(settings.database_url)
    assert "ix_audit_event_id" not in {
        index["name"] for index in inspect(engine).get_indexes("audit_events")
    }
    with make_sessions(engine)() as session:
        assert session.scalar(select(AuditEvent).where(AuditEvent.event == "synthetic"))
    engine.dispose()
    migrate(settings.database_url)
    with TestClient(create_app(settings)) as client:
        assert (
            client.get(
                "/health/live", headers={"Authorization": "Bearer " + settings.owner_token}
            ).status_code
            == 200
        )


def test_migration_rollback_and_reapply_on_disposable_sqlite(tmp_path):
    """A downgrade must undo exactly the newest revision and reapply cleanly."""
    url = f"sqlite:///{tmp_path / 'rollback.sqlite'}"
    upgrade(url, "head")
    engine = make_engine(url)
    tables_before = set(inspect(engine).get_table_names())
    engine.dispose()

    migration(url, "downgrade", "-1")
    engine = make_engine(url)
    tables_after = set(inspect(engine).get_table_names())
    engine.dispose()
    assert "workflow_runs" in tables_before
    assert "workflow_runs" not in tables_after
    assert tables_after < tables_before

    upgrade(url, "head")
    engine = make_engine(url)
    assert set(inspect(engine).get_table_names()) == tables_before
    engine.dispose()


def test_partial_schema_is_rejected_even_when_the_revision_matches(settings):
    """A half-applied restore must not pass the startup gate.

    The recorded Alembic revision can match while a table is missing (interrupted
    restore, hand-edited database). `schema_state` reports ``incomplete`` and the
    application refuses to serve rather than failing later on a missing table.
    """
    from sqlalchemy import text

    from tg_control.api import create_app
    from tg_control.db import schema_state

    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    assert schema_state(engine)["objects"] == "complete"
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE workflow_runs"))
    state = schema_state(engine)
    engine.dispose()
    assert state["state"] == "current" and state["objects"] == "incomplete"
    with pytest.raises(RuntimeError, match="schema is incomplete"):
        with TestClient(create_app(settings)):
            pass
    # Nothing repairs itself: `alembic upgrade head` is a no-op at head, so an
    # incomplete schema requires the documented manual recovery (restore from a
    # verified backup, or rebuild an explicitly disposable database).
    migrate(settings.database_url)
    engine = make_engine(settings.database_url)
    assert schema_state(engine)["objects"] == "incomplete"
    from tg_control.db import Base

    with engine.begin() as connection:
        Base.metadata.drop_all(connection)
        connection.execute(text("DROP TABLE alembic_version"))
    engine.dispose()
    migrate(settings.database_url)
    with TestClient(create_app(settings)) as client:
        assert (
            client.get(
                "/health/ready", headers={"Authorization": "Bearer " + settings.owner_token}
            ).json()["checks"]["schema_objects"]
            == "complete"
        )


def test_unknown_revision_is_reported_and_refused(tmp_path):
    """Revisions this build cannot order are never treated as compatible."""
    from tg_control.db import schema_state

    url = f"sqlite:///{tmp_path / 'unknown.sqlite'}"
    upgrade(url, "head")
    engine = make_engine(url)
    with engine.begin() as connection:
        from sqlalchemy import text

        connection.execute(text("UPDATE alembic_version SET version_num = 'external-fork'"))
    state = schema_state(engine)
    engine.dispose()
    assert state == {"state": "unknown", "revision": "external-fork", "objects": "unknown"}


def test_migration_policy_is_explicit_and_never_automatic():
    from tg_control import db

    assert db.MIGRATION_POLICY == "require_current"
    source = (ROOT / "src" / "tg_control" / "api.py").read_text()
    assert "alembic" not in source.replace("alembic upgrade head", "")
    assert "command.upgrade" not in source and "upgrade(" not in source
