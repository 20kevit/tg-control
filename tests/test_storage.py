"""Integration tests against disposable, Unix-socket-only PostgreSQL.

No external DSN is consumed; no live or owner database can be targeted.
"""
import tempfile
import unittest
from pathlib import Path

try:
    import pgserver
    import psycopg
    from tg_control.storage import PostgresStore, StoredPolicy, StorageError
except ImportError:
    pgserver = None

from tg_control.core import Chat, Grant, Gateway, GatewayError, Message, Request, SyntheticAdapter


@unittest.skipIf(pgserver is None, 'install .[test] for disposable PostgreSQL integration')
class StorageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='tg-control-pg-')
        cls.server = pgserver.get_server(Path(cls.temp.name) / 'db', cleanup_mode='delete')
        cls.dsn = cls.server.get_uri()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()
        cls.temp.cleanup()

    def setUp(self):
        self.store = PostgresStore(self.dsn)
        self.addCleanup(self.store.close)
        self.store.migrate()
        with self.store.transaction() as db:
            # Only our newly created disposable fixture cluster is ever cleaned.
            db.execute('TRUNCATE audit, policies, credentials, clients, accounts RESTART IDENTITY')
            db.execute("UPDATE settings SET value='90' WHERE key='audit_retention_days'")
        self.store.add_account('a', 'fixture account')
        self.store.add_account('b', 'second fixture account')
        self.store.add_client('u')

    def gateway(self):
        self.store.set_grant(Grant('u', 'a', 'read', frozenset({'c'})))
        self.store.set_grant(Grant('u', 'a', 'write', frozenset({'self'})))
        adapter = SyntheticAdapter({'c': Chat('c'), 'self': Chat('self')}, {
            ('c', '1'): Message('1', text='NEVER AUDIT BODY'), ('c', '2'): Message('2', reply_to='1')})
        return Gateway({'a': adapter}, StoredPolicy(self.store), audit_sink=self.store), adapter

    def test_migrate_idempotent_and_checksum(self):
        self.store.migrate()
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0], 1)
            db.execute("UPDATE schema_migrations SET checksum='modified' WHERE version=1")
        try:
            with self.assertRaisesRegex(GatewayError, 'migration_checksum_mismatch'):
                self.store.migrate()
        finally:
            # Restore checksum so remaining tests can verify the same cluster.
            from importlib.resources import files
            import hashlib
            checksum = hashlib.sha256(files('tg_control').joinpath('migrations/001_initial.sql').read_bytes()).hexdigest()
            with self.store.transaction() as db:
                db.execute('UPDATE schema_migrations SET checksum=%s', (checksum,))

    def test_credential_lifecycle_survives_reconnect(self):
        token = self.store.issue('u')
        other = PostgresStore(self.dsn)
        self.addCleanup(other.close)
        self.assertEqual(other.authenticate(token), 'u')
        replacement = other.rotate(token)
        self.assertIsNone(self.store.authenticate(token))
        self.assertIsNone(self.store.authenticate(replacement + 'wrong'))
        self.assertEqual(self.store.authenticate(replacement), 'u')
        self.store.revoke(replacement.split('.')[0])
        self.assertIsNone(other.authenticate(replacement))
        with self.store.transaction() as db:
            rows = db.execute('SELECT salt,digest FROM credentials').fetchall()
        self.assertNotIn(token, repr(rows))
        self.assertTrue(all(len(r[0]) == 16 and len(r[1]) == 64 for r in rows))

    def test_disable_client_blocks_auth_and_policy(self):
        gateway, _ = self.gateway()
        token = self.store.issue('u')
        self.store.disable_client('u')
        self.assertIsNone(self.store.authenticate(token))
        with self.assertRaisesRegex(GatewayError, 'permission_denied'):
            gateway.execute('u', Request('a', 'read', 'c', '1'))
        with self.assertRaisesRegex(GatewayError, 'invalid_client'):
            self.store.issue('u')

    def test_gateway_persistent_policy_audit_and_save(self):
        gateway, adapter = self.gateway()
        gateway.execute('u', Request('a', 'save', 'c', '2'))
        self.assertEqual(adapter.sent[0][0], 'self')
        events = self.store.audit_view('u')
        self.assertEqual([r['outcome'] for r in events], ['ok', 'accepted'])
        self.assertNotIn('NEVER AUDIT BODY', repr(events))
        self.assertEqual(self.store.audit_view('other'), [])
        self.store.set_grant(Grant('u', 'a', 'read', frozenset({'c'}), exclusions=frozenset({'c'})))
        with self.assertRaisesRegex(GatewayError, 'permission_denied'):
            gateway.execute('u', Request('a', 'read', 'c', '1'))
        self.assertFalse(StoredPolicy(self.store).allows('u', 'b', 'read', Chat('c')))

    def test_transaction_rollback_and_parameter_binding(self):
        hostile = "client'); DROP TABLE accounts; --"
        self.store.add_client(hostile)
        self.store.set_grant(Grant(hostile, 'a', 'read', frozenset({'c'})))
        self.assertTrue(self.store.load_policy(hostile, 'a').allows(hostile, 'a', 'read', Chat('c')))
        with self.assertRaises(RuntimeError):
            with self.store.transaction() as db:
                db.execute("INSERT INTO clients VALUES ('rollback',false)")
                raise RuntimeError('fixture rollback')
        with self.store.transaction() as db:
            self.assertIsNone(db.execute("SELECT id FROM clients WHERE id='rollback'").fetchone())
            self.assertEqual(db.execute('SELECT count(*) FROM accounts').fetchone()[0], 2)

    def test_settings_and_retention(self):
        self.assertEqual(self.store.settings()['audit_retention_days'], 90)
        self.store.set_setting('audit_retention_days', 30)
        with self.assertRaisesRegex(GatewayError, 'invalid_setting'):
            self.store.set_setting('compliance_approved', True)
        with self.store.transaction() as db:
            db.execute("INSERT INTO audit(client_id,account_id,chat_id,operation,outcome,created_at) "
                       "VALUES ('u','a','c','read','ok',now()-interval '31 days')")
        self.assertEqual(self.store.prune_audit(), 1)
        self.assertEqual(self.store.audit_view('u'), [])

    def test_unavailable_audit_blocks_side_effect(self):
        gateway, adapter = self.gateway()
        self.store.close()
        with self.assertRaises(StorageError):
            gateway.execute('u', Request('a', 'save', 'c', '2'))
        self.assertEqual(adapter.sent, [])

    def test_policy_validation(self):
        with self.assertRaisesRegex(GatewayError, 'invalid_policy'):
            self.store.set_grant(Grant('u', 'a', 'read', public_types=frozenset({'private'})))

    def test_concurrent_rotation_only_one_wins(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        token = self.store.issue('u')
        barrier = threading.Barrier(2)
        def rotate():
            connection = PostgresStore(self.dsn)
            try:
                barrier.wait(timeout=5)
                return connection.rotate(token)
            except GatewayError as error:
                return error.code
            finally:
                connection.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: rotate(), range(2)))
        self.assertEqual(results.count('invalid_credential'), 1)
        winner = next(r for r in results if r != 'invalid_credential')
        self.assertEqual(self.store.authenticate(winner), 'u')
        self.assertIsNone(self.store.authenticate(token))

    def test_server_has_no_tcp_listener(self):
        with self.store.transaction() as db:
            self.assertEqual(db.execute('SHOW listen_addresses').fetchone()[0], '')

    def test_management_views_exclude_credential_material(self):
        token = self.store.issue('u')
        self.assertEqual([a['id'] for a in self.store.accounts()], ['a', 'b'])
        self.assertEqual(self.store.clients(), [{'id': 'u', 'disabled': False}])
        metadata = self.store.credential_metadata('u')
        self.assertEqual(metadata, [{'id': token.split('.')[0], 'revoked': False}])
        self.assertEqual(self.store.credential_metadata('other'), [])
        self.assertNotIn(token, repr(metadata))
