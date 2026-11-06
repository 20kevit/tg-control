"""Internal empty-target probe, run with the same isolated libpq environment as pg_restore."""

from __future__ import annotations

import psycopg


def main() -> int:
    try:
        with psycopg.connect("service=tgcontrol") as db:
            with db.cursor() as cursor:
                # An unexpected target must not shadow catalog relations/functions.
                cursor.execute("SET search_path TO pg_catalog, pg_temp")
                cursor.execute("""
SELECT
  EXISTS (SELECT 1 FROM pg_namespace n WHERE n.nspname NOT IN
    ('pg_catalog', 'information_schema', 'public') AND left(n.nspname, 3) <> 'pg_')
  OR EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f'))
  OR EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_operator o JOIN pg_namespace n ON n.oid = o.oprnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_opclass o JOIN pg_namespace n ON n.oid = o.opcnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_opfamily o JOIN pg_namespace n ON n.oid = o.opfnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_collation c JOIN pg_namespace n ON n.oid = c.collnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_conversion c JOIN pg_namespace n ON n.oid = c.connamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_ts_config t JOIN pg_namespace n ON n.oid = t.cfgnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_ts_dict t JOIN pg_namespace n ON n.oid = t.dictnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_ts_parser t JOIN pg_namespace n ON n.oid = t.prsnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_ts_template t JOIN pg_namespace n ON n.oid = t.tmplnamespace
    WHERE n.nspname = 'public')
  OR EXISTS (SELECT 1 FROM pg_extension e WHERE e.extname <> 'plpgsql')
  OR EXISTS (SELECT 1 FROM pg_largeobject_metadata)
  OR EXISTS (SELECT 1 FROM pg_event_trigger)
  OR EXISTS (SELECT 1 FROM pg_publication)
  OR EXISTS (SELECT 1 FROM pg_subscription s JOIN pg_database d ON d.oid = s.subdbid
    WHERE d.datname = current_database())
  OR EXISTS (SELECT 1 FROM pg_foreign_data_wrapper)
  OR EXISTS (SELECT 1 FROM pg_foreign_server)
  OR EXISTS (SELECT 1 FROM pg_default_acl)
  OR EXISTS (SELECT 1 FROM pg_cast WHERE oid >= 16384)
  OR EXISTS (SELECT 1 FROM pg_transform)
  OR EXISTS (SELECT 1 FROM pg_language WHERE lanname NOT IN ('internal', 'c', 'sql', 'plpgsql'))
  OR EXISTS (SELECT 1 FROM pg_db_role_setting s JOIN pg_database d ON d.oid = s.setdatabase
    WHERE d.datname = current_database())
                """)
                return 2 if cursor.fetchone()[0] else 0
    except Exception:
        # Connection errors can include addresses, credentials, or SQL context.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
