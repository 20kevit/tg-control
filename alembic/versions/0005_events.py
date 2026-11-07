"""Durable normalized event inbox, subscriptions, attempts and dead letters."""

import sqlalchemy as sa

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "events",
        sa.Column("seq", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("id", sa.String(36), nullable=False, unique=True),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("aggregate_type", sa.String(32), nullable=True),
        sa.Column("aggregate_id", sa.String(128), nullable=True),
        sa.Column("chat_id", sa.String(128), nullable=True),
        sa.Column("correlation_id", sa.String(36), nullable=True),
        sa.Column("causation_id", sa.String(36), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False, unique=True),
    )
    op.create_index("ix_events_account_id", "events", ["account_id"])
    op.create_index("ix_events_account_seq", "events", ["account_id", "seq"])
    op.create_index("ix_events_type_seq", "events", ["event_type", "seq"])
    op.create_table(
        "event_subscriptions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "account_id",
            sa.String(36),
            sa.ForeignKey("accounts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("event_types", sa.JSON(), nullable=False),
        sa.Column("chat_ids", sa.JSON(), nullable=False),
        sa.Column("max_inflight", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("cursor_seq", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_subscriptions_client_id", "event_subscriptions", ["client_id"])
    op.create_index(
        "ix_event_subscriptions_client", "event_subscriptions", ["client_id", "account_id"]
    )
    op.create_index(
        "ix_event_subscriptions_name",
        "event_subscriptions",
        ["client_id", "account_id", "name"],
        unique=True,
    )
    op.create_table(
        "event_attempts",
        sa.Column(
            "subscription_id",
            sa.String(36),
            sa.ForeignKey("event_subscriptions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.String(64), nullable=True),
    )
    op.create_table(
        "event_dead_letters",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "subscription_id",
            sa.String(36),
            sa.ForeignKey("event_subscriptions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_seq", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_event_dead_letters_subscription_id", "event_dead_letters", ["subscription_id"]
    )
    op.create_index(
        "ix_event_dead_letters_event",
        "event_dead_letters",
        ["subscription_id", "event_id"],
        unique=True,
    )


def downgrade():
    # Destructive; explicit approval and a verified backup are required.
    op.drop_index("ix_event_dead_letters_event", table_name="event_dead_letters")
    op.drop_index("ix_event_dead_letters_subscription_id", table_name="event_dead_letters")
    op.drop_table("event_dead_letters")
    op.drop_table("event_attempts")
    op.drop_index("ix_event_subscriptions_name", table_name="event_subscriptions")
    op.drop_index("ix_event_subscriptions_client", table_name="event_subscriptions")
    op.drop_index("ix_event_subscriptions_client_id", table_name="event_subscriptions")
    op.drop_table("event_subscriptions")
    op.drop_index("ix_events_type_seq", table_name="events")
    op.drop_index("ix_events_account_seq", table_name="events")
    op.drop_index("ix_events_account_id", table_name="events")
    op.drop_table("events")
