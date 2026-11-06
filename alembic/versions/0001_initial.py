"""Create gateway metadata; Telegram data remains outside this schema."""

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "accounts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("alias", sa.String(80), nullable=False, unique=True),
        sa.Column("phone_encrypted", sa.String(512), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "clients",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False, unique=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "credentials",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("token_digest", sa.String(64), nullable=False),
        sa.Column("revoked", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_credentials_client_id", "credentials", ["client_id"])
    op.create_table(
        "grants",
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("read_scope", sa.JSON(), nullable=False),
        sa.Column("write_scope", sa.JSON(), nullable=False),
        sa.Column("admin_scope", sa.JSON(), nullable=False),
    )
    op.create_table(
        "settings",
        sa.Column("key", sa.String(80), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("actor_kind", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(36)),
        sa.Column("event", sa.String(64), nullable=False),
        sa.Column("account_id", sa.String(36)),
        sa.Column("chat_id", sa.String(128)),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
    )
    op.create_index("ix_audit_timestamp_id", "audit_events", ["timestamp", "id"])


def downgrade():
    # Destructive; never run on a real installation without explicit approval and backup.
    op.drop_index("ix_audit_timestamp_id", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_table("settings")
    op.drop_table("grants")
    op.drop_index("ix_credentials_client_id", table_name="credentials")
    op.drop_table("credentials")
    op.drop_table("clients")
    op.drop_table("accounts")
