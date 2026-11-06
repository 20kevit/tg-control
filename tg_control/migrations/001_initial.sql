-- Additive PostgreSQL baseline; applied transactionally by storage.migrate.
CREATE TABLE accounts (id text PRIMARY KEY, alias text NOT NULL UNIQUE);
CREATE TABLE clients (id text PRIMARY KEY, disabled boolean NOT NULL DEFAULT false);
CREATE TABLE credentials (
 id text PRIMARY KEY, client_id text NOT NULL REFERENCES clients(id),
 salt bytea NOT NULL, digest bytea NOT NULL, revoked boolean NOT NULL DEFAULT false
);
CREATE TABLE policies (
 client_id text NOT NULL REFERENCES clients(id), account_id text NOT NULL REFERENCES accounts(id),
 permission text NOT NULL CHECK (permission IN ('read','write','admin')),
 chats jsonb NOT NULL DEFAULT '[]', public_types jsonb NOT NULL DEFAULT '[]',
 exclusions jsonb NOT NULL DEFAULT '[]', PRIMARY KEY(client_id,account_id,permission)
);
CREATE TABLE audit (
 id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 client_id text NOT NULL, account_id text NOT NULL, chat_id text NOT NULL,
 operation text NOT NULL, outcome text NOT NULL, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_time ON audit(created_at);
CREATE TABLE settings (key text PRIMARY KEY, value jsonb NOT NULL);
INSERT INTO settings VALUES ('audit_retention_days', '90'), ('message_cache_seconds', '0'), ('media_retention_seconds', '0');
