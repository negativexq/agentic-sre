"""Add alert-channel coverage segments and their poll audit.

Revision ID: 0025_alert_coverage
Revises: 0024_evidence_requirements

M21 contract §10.2 (amendment 4): the alert channel's observation coverage is its
own evidence. A read-only poller records contiguous segments; a run boundary
freezes one of them later. Nothing in RCA reads these tables yet.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0025_alert_coverage"
down_revision = "0024_evidence_requirements"
branch_labels = None
depends_on = None

SEGMENTS = "alert_coverage_segments"
POLLS = "alert_coverage_polls"
STATUSES = ("OPEN", "CLOSED_FAILURE", "BROKEN_GAP")
SOURCE_STATUS_INDEX = "ix_alert_coverage_segments_source_status"


def upgrade() -> None:
    # Revision 0001 creates tables from live metadata, so a fresh database may
    # already have these tables while databases upgraded from 0024 do not.
    tables = set(inspect(op.get_bind()).get_table_names())
    if SEGMENTS not in tables:
        op.create_table(
            SEGMENTS,
            sa.Column("segment_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("last_success_at", sa.DateTime(), nullable=False),
            sa.Column("ended_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(16), nullable=False),
            sa.CheckConstraint(
                "status IN (" + ", ".join(f"'{item}'" for item in STATUSES) + ")",
                name="ck_alert_coverage_segment_status",
            ),
        )
        op.create_index(SOURCE_STATUS_INDEX, SEGMENTS, ["source", "status"])
    if POLLS not in tables:
        op.create_table(
            POLLS,
            sa.Column("poll_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("source", sa.String(32), nullable=False),
            sa.Column(
                "segment_id",
                sa.Integer(),
                sa.ForeignKey(f"{SEGMENTS}.segment_id", name="fk_alert_coverage_poll_segment"),
                nullable=True,
            ),
            sa.Column("attempted_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=False),
            sa.Column("success", sa.Boolean(), nullable=False),
            sa.Column("error_type", sa.String(255), nullable=True),
            sa.Column("active_alerts", sa.Integer(), nullable=True),
        )


def downgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if POLLS in tables:
        op.drop_table(POLLS)
    if SEGMENTS in tables:
        indexes = {item["name"] for item in inspect(op.get_bind()).get_indexes(SEGMENTS)}
        if SOURCE_STATUS_INDEX in indexes:
            op.drop_index(SOURCE_STATUS_INDEX, table_name=SEGMENTS)
        op.drop_table(SEGMENTS)
