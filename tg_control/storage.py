"""PostgreSQL metadata boundary. Never persists message bodies or raw credentials.

Only trusted owner-side code may call management methods. No remote management
interface is implied by possession of a Telegram Admin permission.
"""
from contextlib import contextmanager
from importlib.resources import files
import hashlib
import hmac
import secrets
import threading

import psycopg
from psycopg.types.json import Jsonb

from .core import GatewayError, Grant, Policy
from .security import Credentials


class StorageError(GatewayError):
    def __init__(self):
        super().__init__('storage_unavailable')


def identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise GatewayError('invalid_identifier')
    return value


class PostgresStore:
    def __init__(self, dsn):
        # Connection errors must not leak DSNs/passwords to interfaces.
        try:
            self.connection = psycopg.connect(dsn, autocommit=True, connect_timeout=5,
                                               options="-c statement_timeout=5000 -c lock_timeout=2000 "
                                                       "-c idle_in_transaction_session_timeout=10000")
        except psycopg.Error:
            raise StorageError() from None
        self.lock = threading.RLock()

    def close(self):
        self.connection.close()

    @contextmanager
    def transaction(self):
        try:
            with self.lock, self.connection.transaction():
                yield self.connection
        except psycopg.Error:
            raise StorageError() from None

    def migrate(self):
        """Additive migration with checksum and concurrent-run serialization."""
        sql = files('tg_control').joinpath('migrations/001_initial.sql').read_text()
        checksum = hashlib.sha256(sql.encode()).hexdigest()
        with self.transaction() as db:
            db.execute('SELECT pg_advisory_xact_lock(750149432)')
            db.execute('CREATE TABLE IF NOT EXISTS schema_migrations '
                       '(version integer PRIMARY KEY, checksum text NOT NULL)')
            row = db.execute('SELECT checksum FROM schema_migrations WHERE version=1').fetchone()
            if row:
                if row[0] != checksum:
                    raise GatewayError('migration_checksum_mismatch')
                return
            db.execute(sql)
            db.execute('INSERT INTO schema_migrations VALUES (1,%s)', (checksum,))

    def add_account(self, account, alias):
        with self.transaction() as db:
            db.execute('INSERT INTO accounts(id,alias) VALUES (%s,%s)',
                       (identifier(account), identifier(alias)))

    def add_client(self, client):
        with self.transaction() as db:
            db.execute('INSERT INTO clients(id) VALUES (%s)', (identifier(client),))

    def accounts(self):
        with self.transaction() as db:
            return [dict(zip(('id', 'alias'), row)) for row in
                    db.execute('SELECT id,alias FROM accounts ORDER BY id').fetchall()]

    def clients(self):
        with self.transaction() as db:
            return [dict(zip(('id', 'disabled'), row)) for row in
                    db.execute('SELECT id,disabled FROM clients ORDER BY id').fetchall()]

    def credential_metadata(self, client):
        with self.transaction() as db:
            return [dict(zip(('id', 'revoked'), row)) for row in
                    db.execute('SELECT id,revoked FROM credentials WHERE client_id=%s ORDER BY id',
                               (identifier(client),)).fetchall()]

    def disable_client(self, client):
        with self.transaction() as db:
            db.execute('UPDATE clients SET disabled=true WHERE id=%s', (identifier(client),))
            db.execute('UPDATE credentials SET revoked=true WHERE client_id=%s', (client,))

    def set_grant(self, grant):
        if not isinstance(grant, Grant) or grant.permission not in {'read', 'write', 'admin'}:
            raise GatewayError('invalid_policy')
        if not grant.public_types <= {'public_group', 'public_channel'}:
            raise GatewayError('invalid_policy')
        scopes = [grant.chats, grant.public_types, grant.exclusions]
        for scope in scopes:
            if len(scope) > 10000:
                raise GatewayError('invalid_policy')
            for entry in scope:
                identifier(entry)
        with self.transaction() as db:
            db.execute('INSERT INTO policies VALUES (%s,%s,%s,%s,%s,%s) '
                       'ON CONFLICT(client_id,account_id,permission) DO UPDATE SET '
                       'chats=EXCLUDED.chats, public_types=EXCLUDED.public_types, exclusions=EXCLUDED.exclusions',
                       (identifier(grant.client), identifier(grant.account), grant.permission,
                        *(Jsonb(sorted(s)) for s in scopes)))

    def load_policy(self, client, account):
        with self.transaction() as db:
            rows = db.execute('SELECT p.permission,p.chats,p.public_types,p.exclusions FROM policies p '
                              'JOIN clients c ON c.id=p.client_id WHERE p.client_id=%s '
                              'AND p.account_id=%s AND NOT c.disabled',
                              (identifier(client), identifier(account))).fetchall()
        return Policy([Grant(client, account, r[0], *(frozenset(s) for s in r[1:])) for r in rows])

    def _issue(self, db, client):
        token_id = secrets.token_hex(16)
        token = token_id + '.' + secrets.token_urlsafe(32)
        salt = secrets.token_bytes(16)
        db.execute('INSERT INTO credentials(id,client_id,salt,digest) VALUES (%s,%s,%s,%s)',
                   (token_id, client, salt, Credentials.digest(token, salt)))
        return token

    def issue(self, client):
        with self.transaction() as db:
            row = db.execute('SELECT id FROM clients WHERE id=%s AND NOT disabled FOR UPDATE',
                             (identifier(client),)).fetchone()
            if not row:
                raise GatewayError('invalid_client')
            return self._issue(db, client)

    @staticmethod
    def _credential(db, token, *, lock=False):
        if not isinstance(token, str) or not 1 <= len(token) <= 256:
            return None
        row = db.execute('SELECT c.id,c.client_id,c.salt,c.digest FROM credentials c '
                         'JOIN clients u ON u.id=c.client_id WHERE c.id=%s '
                         'AND NOT c.revoked AND NOT u.disabled' + (' FOR UPDATE OF c' if lock else ''),
                         (token.split('.')[0],)).fetchone()
        if row and hmac.compare_digest(bytes(row[3]), Credentials.digest(token, bytes(row[2]))):
            return row
        return None

    def authenticate(self, token):
        with self.transaction() as db:
            row = self._credential(db, token)
            return row[1] if row else None

    def rotate(self, token):
        with self.transaction() as db:
            row = self._credential(db, token)
            if not row:
                raise GatewayError('invalid_credential')
            # Consistent client-then-credential lock order with disable_client/issue.
            client = db.execute('SELECT id FROM clients WHERE id=%s AND NOT disabled FOR UPDATE',
                                (row[1],)).fetchone()
            if not client:
                raise GatewayError('invalid_client')
            row = self._credential(db, token, lock=True)
            if not row:
                raise GatewayError('invalid_credential')
            replacement = self._issue(db, row[1])
            db.execute('UPDATE credentials SET revoked=true WHERE id=%s', (row[0],))
            return replacement

    def revoke(self, credential_id):
        # Management takes a non-secret ID, never needs a raw credential.
        with self.transaction() as db:
            db.execute('UPDATE credentials SET revoked=true WHERE id=%s', (identifier(credential_id),))

    def append(self, event):
        # Strict allowlist: extra fields (including content) cannot enter SQL.
        with self.transaction() as db:
            db.execute('INSERT INTO audit(client_id,account_id,chat_id,operation,outcome) '
                       'VALUES (%s,%s,%s,%s,%s)', tuple(identifier(event[k]) for k in
                        ('client', 'account', 'chat', 'operation', 'outcome')))

    def audit_view(self, client, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise GatewayError('invalid_limit')
        with self.transaction() as db:
            rows = db.execute('SELECT account_id,chat_id,operation,outcome,created_at FROM audit '
                              'WHERE client_id=%s ORDER BY id DESC LIMIT %s',
                              (identifier(client), limit)).fetchall()
        return [dict(zip(('account', 'chat', 'operation', 'outcome', 'time'), r)) for r in rows]

    def settings(self):
        with self.transaction() as db:
            return dict(db.execute('SELECT key,value FROM settings').fetchall())

    def set_setting(self, key, value):
        bounds = {'audit_retention_days': (1, 3650), 'message_cache_seconds': (0, 86400),
                  'media_retention_seconds': (0, 31536000)}
        if key not in bounds or type(value) is not int or not bounds[key][0] <= value <= bounds[key][1]:
            raise GatewayError('invalid_setting')
        with self.transaction() as db:
            db.execute('UPDATE settings SET value=%s WHERE key=%s', (Jsonb(value), key))

    def prune_audit(self):
        # Deliberately explicit owner maintenance action, not automatic migration.
        with self.transaction() as db:
            days = db.execute("SELECT value FROM settings WHERE key='audit_retention_days'").fetchone()[0]
            if type(days) is not int or not 1 <= days <= 3650:
                raise GatewayError('invalid_setting')
            return db.execute("DELETE FROM audit WHERE created_at < now() - %s * interval '1 day'",
                              (days,)).rowcount


class StoredPolicy:
    def __init__(self, store):
        self.store = store

    def allows(self, client, account, permission, chat):
        return self.store.load_policy(client, account).allows(client, account, permission, chat)
