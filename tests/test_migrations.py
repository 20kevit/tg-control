"""Run actual migrations against SQLite and an isolated local PostgreSQL socket."""

import os
import subprocess
import sys
from pathlib import Path

import pgserver
from sqlalchemy import inspect

from tg_control.db import make_engine

ROOT = Path(__file__).resolve().parents[1]


def migrate(url):
    env = os.environ.copy()
    env["TG_DATABASE_URL"] = url
    env.pop("TG_DATABASE_URL_FILE", None)
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
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
    engine.dispose()


def test_sqlite_migration(tmp_path):
    migrate(f"sqlite:///{tmp_path / 'migration.sqlite'}")


def test_postgres_migration_and_storage(tmp_path):
    # pgserver uses an isolated temporary Unix socket with no TCP listener.
    server = pgserver.get_server(tmp_path / "pgdata", cleanup_mode="delete")
    try:
        url = server.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
        migrate(url)
        engine = make_engine(url)
        from tg_control.auth import authenticate, rotate_credential
        from tg_control.db import Client, make_sessions

        sessions = make_sessions(engine)
        with sessions() as session:
            client = Client(name="synthetic postgres client")
            session.add(client)
            session.flush()
            token = rotate_credential(session, client.id)
            session.commit()
            assert authenticate(session, token) is not None
        engine.dispose()
    finally:
        server.cleanup()
