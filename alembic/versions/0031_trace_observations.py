"""Persist the spans captured for a diagnosis and how complete each read was.

Revision ID: 0031_trace_observations
Revises: 0030_resolved_trigger

live-trace-design.md §3: the base trace read is frozen by the run's manifest like the logs.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0031_trace_observations"
down_revision = "0030_resolved_trigger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 0001 builds tables from the current models, so a fresh database may already have them
    tables = inspect(op.get_bind()).get_table_names()
    if "trace_observations" not in tables:
        op.create_table(
            "trace_observations",
            sa.Column("observation_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "incident_id",
                sa.Uuid(),
                sa.ForeignKey("incidents.incident_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("service", sa.String(length=255), nullable=False),
            sa.Column("event_at", sa.DateTime(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("span", sa.JSON(), nullable=False),
            sa.Column("dedup_key", sa.String(length=64), nullable=False),
            sa.Column("source_system", sa.String(length=255), nullable=False),
            sa.UniqueConstraint(
                "incident_id", "dedup_key", name="uq_trace_observation_incident_key"
            ),
        )
        op.create_index("ix_trace_observations_incident_id", "trace_observations", ["incident_id"])
        op.create_index("ix_trace_observations_event_at", "trace_observations", ["event_at"])
        op.create_index("ix_trace_observations_observed_at", "trace_observations", ["observed_at"])
    if "trace_captures" not in tables:
        op.create_table(
            "trace_captures",
            sa.Column("capture_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "incident_id",
                sa.Uuid(),
                sa.ForeignKey("incidents.incident_id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("namespace", sa.String(length=253), nullable=False),
            sa.Column("service", sa.String(length=255), nullable=False),
            sa.Column("starts_at", sa.DateTime(), nullable=False),
            sa.Column("ends_at", sa.DateTime(), nullable=False),
            sa.Column("completeness", sa.String(length=32), nullable=False),
            sa.Column("spans", sa.Integer(), nullable=False),
            sa.Column("error", sa.String(length=255), nullable=False),
            sa.Column("captured_at", sa.DateTime(), nullable=False),
        )
        op.create_index("ix_trace_captures_incident_id", "trace_captures", ["incident_id"])


def downgrade() -> None:
    tables = inspect(op.get_bind()).get_table_names()
    if "trace_captures" in tables:
        op.drop_index("ix_trace_captures_incident_id", table_name="trace_captures")
        op.drop_table("trace_captures")
    if "trace_observations" in tables:
        for index in ("observed_at", "event_at", "incident_id"):
            op.drop_index(f"ix_trace_observations_{index}", table_name="trace_observations")
        op.drop_table("trace_observations")
