"""Reserve one-time mutation keys before performing effects."""

import sqlalchemy as sa

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "execution_keys",
        sa.Column(
            "client_id",
            sa.String(36),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("key", sa.String(36), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    # Destructive; explicit approval and a verified backup are required.
    op.drop_table("execution_keys")
