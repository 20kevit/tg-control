"""Separate the owner actor from the client affected by a management change."""

import sqlalchemy as sa

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("audit_events", sa.Column("subject_client_id", sa.String(36), nullable=True))


def downgrade():
    # Destructive; explicit approval and a verified backup are required.
    op.drop_column("audit_events", "subject_client_id")
