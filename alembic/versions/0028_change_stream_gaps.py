"""Persist losses of change-stream continuity with their scope and interval.

Revision ID: 0028_change_stream_gaps
Revises: 0027_connector_observed_at

late-evidence-design.md §4.2: the source-continuity dimension of a diagnosis's coverage record reads these.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0028_change_stream_gaps"
down_revision = "0027_connector_observed_at"
branch_labels = None
depends_on = None

TABLE = "change_stream_gaps"


def upgrade() -> None:
    # 0001 builds tables from the current models, so a fresh database may already have it
    if TABLE in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("gap_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("namespace", sa.String(length=253), nullable=True),
        sa.Column("kind", sa.String(length=128), nullable=True),
        sa.Column("since", sa.DateTime(), nullable=True),
        sa.Column("at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_change_stream_gaps_at", TABLE, ["at"])


def downgrade() -> None:
    if TABLE in inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_change_stream_gaps_at", table_name=TABLE)
        op.drop_table(TABLE)
