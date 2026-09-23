"""Persist structured bounded-investigation run artifacts.

Revision ID: 0015_investigation_run_artifacts
Revises: 0014_email_delivery_report_key
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0015_investigation_run_artifacts"
down_revision = "0014_email_delivery_report_key"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "investigation_runs" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "investigation_runs",
        sa.Column("diagnosis_run_id", sa.String(64), primary_key=True),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("incidents.incident_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("artifact_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("document", sa.JSON(), nullable=False),
    )
    op.create_index("ix_investigation_runs_incident_id", "investigation_runs", ["incident_id"])


def downgrade() -> None:
    if "investigation_runs" not in inspect(op.get_bind()).get_table_names():
        return
    op.drop_index("ix_investigation_runs_incident_id", table_name="investigation_runs")
    op.drop_table("investigation_runs")
