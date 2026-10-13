"""Preserve source log correlation assertions without changing RCA authority.

Revision ID: 0033_log_correlation_attributes
Revises: 0032_connectors
"""

import sqlalchemy as sa
from alembic import op

revision = "0033_log_correlation_attributes"
down_revision = "0032_connectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 creates current metadata for fresh databases.
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("log_observations")}
    if "attributes" not in columns:
        op.add_column("log_observations", sa.Column("attributes", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("log_observations", "attributes")
