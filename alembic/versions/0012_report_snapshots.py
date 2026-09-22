"""Immutable incident report snapshots pinned to a diagnosis run.

Revision ID: 0012_report_snapshots
Revises: 0011_diagnosis_run_id
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0012_report_snapshots"
down_revision = "0011_diagnosis_run_id"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("report_snapshots"):
        return
    op.create_table(
        "report_snapshots",
        sa.Column("report_id", sa.String(64), primary_key=True),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("incidents.incident_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("diagnosis_run_id", sa.String(64), nullable=True),
        sa.Column("report_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
    )
    op.create_index("ix_report_snapshots_incident_id", "report_snapshots", ["incident_id"])


def downgrade() -> None:
    if _has_table("report_snapshots"):
        op.drop_index("ix_report_snapshots_incident_id", table_name="report_snapshots")
        op.drop_table("report_snapshots")
