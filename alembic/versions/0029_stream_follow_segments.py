"""Persist how far each control-plane process followed the change stream.

Revision ID: 0029_stream_follow_segments
Revises: 0028_change_stream_gaps

late-evidence-design.md §4.2: a restarted control plane keeps the instant the stream was followed from when it
provably resumes where the previous process stopped.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0029_stream_follow_segments"
down_revision = "0028_change_stream_gaps"
branch_labels = None
depends_on = None

TABLE = "stream_follow_segments"


def upgrade() -> None:
    # 0001 builds tables from the current models, so a fresh database may already have it
    if TABLE in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("segment_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("epoch", sa.String(length=128), nullable=False),
        sa.Column("first_seq", sa.Integer(), nullable=False),
        sa.Column("last_seq", sa.Integer(), nullable=False),
        sa.Column("followed_since", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_stream_follow_epoch", TABLE, ["epoch", "last_seq"])


def downgrade() -> None:
    if TABLE in inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_stream_follow_epoch", table_name=TABLE)
        op.drop_table(TABLE)
