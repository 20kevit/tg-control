"""Synthetic PostgreSQL objects that must block an offline restore target."""

import pgserver
import psycopg
import pytest
from sqlalchemy.engine import make_url

from tg_control.backup import BackupError, _connection, _empty_database, _service


def test_catalog_probe_rejects_non_table_objects(tmp_path):
    server = pgserver.get_server(tmp_path / "catalog-pgdata", cleanup_mode="delete")
    cases = {
        "empty_reference": None,
        "operator_target": (
            "CREATE OPERATOR public.=== "
            "(PROCEDURE = pg_catalog.int4eq, LEFTARG = integer, RIGHTARG = integer)"
        ),
        "textsearch_target": (
            "CREATE TEXT SEARCH CONFIGURATION public.synthetic_search (COPY = pg_catalog.simple)"
        ),
        "foreign_target": "CREATE FOREIGN DATA WRAPPER synthetic_fdw",
        "subscription_target": (
            "CREATE SUBSCRIPTION synthetic_sub "
            "CONNECTION 'host=127.0.0.1 port=1 dbname=synthetic' "
            "PUBLICATION synthetic_pub WITH (connect = false)"
        ),
        "setting_target": "ALTER DATABASE setting_target SET search_path TO public",
    }
    try:
        with psycopg.connect(server.get_uri(), autocommit=True) as db:
            for name in cases:
                db.execute(f"CREATE DATABASE {name}")
            db.execute("CREATE ROLE synthetic_restore LOGIN")
        for name, statement in cases.items():
            if statement:
                with psycopg.connect(server.get_uri(name), autocommit=True) as db:
                    db.execute(statement)
            url_file = tmp_path / f"{name}.url"
            url_file.write_text(server.get_uri(name))
            url_file.chmod(0o600)
            scratch = tmp_path / f"{name}-scratch"
            scratch.mkdir(mode=0o700)
            with _service(_connection(url_file), scratch) as env:
                assert _empty_database(env) is (name == "empty_reference")
        # The probe selects only nonsensitive catalog columns; a non-superuser
        # can inspect an empty target and detect the synthetic subscription.
        for name in ("empty_reference", "subscription_target"):
            restricted = tmp_path / f"restricted-{name}.url"
            restricted.write_text(
                make_url(server.get_uri(name))
                .set(username="synthetic_restore")
                .render_as_string(hide_password=False)
            )
            restricted.chmod(0o600)
            scratch = tmp_path / f"restricted-{name}-scratch"
            scratch.mkdir(mode=0o700)
            with _service(_connection(restricted), scratch) as env:
                assert _empty_database(env) is (name == "empty_reference")

    finally:
        server.cleanup()


def test_catalog_probe_fails_closed_when_target_cannot_be_inspected(tmp_path):
    scratch = tmp_path / "scratch"
    scratch.mkdir(mode=0o700)
    connection = {"host": str(tmp_path / "missing-unix-socket"), "dbname": "synthetic"}
    with _service(connection, scratch) as env:
        with pytest.raises(BackupError, match="could not be verified"):
            _empty_database(env)
