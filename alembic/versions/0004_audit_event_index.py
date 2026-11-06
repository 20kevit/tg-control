"""Index owner-filtered audit events without altering existing records."""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index("ix_audit_event_id", "audit_events", ["event", "id"])


def downgrade():
    op.drop_index("ix_audit_event_id", table_name="audit_events")
