"""The Connector registry: allow-list and one-time enrollment state.

Revision ID: 0032_connectors
Revises: 0031_trace_observations

connector-install-design.md §A8.2.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0032_connectors"
down_revision = "0031_trace_observations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 builds tables from the current models, so a fresh database may already have it
    if "connectors" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "connectors",
        sa.Column("connector_id", sa.String(length=63), primary_key=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("token_used_at", sa.DateTime(), nullable=True),
        sa.Column("cert_serial", sa.String(length=64), nullable=True),
        sa.Column("cert_not_after", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'disabled')", name="ck_connector_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("connectors")
