"""Persist bounded normalized Loki observations for diagnosis replay.

Revision ID: 0009_log_observations
Revises: 0008_alert_occurrences
"""

from alembic import op
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint, inspect

revision = "0009_log_observations"
down_revision = "0008_alert_occurrences"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "log_observations" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "log_observations",
        Column("observation_id", Integer(), primary_key=True, autoincrement=True),
        Column(
            "incident_id",
            ForeignKey("incidents.incident_id", ondelete="CASCADE"),
            nullable=False,
        ),
        Column("service", String(255), nullable=False),
        Column("event_at", DateTime(timezone=True), nullable=True),
        Column("observed_at", DateTime(timezone=True), nullable=False),
        Column("severity", String(32), nullable=False),
        Column("message", String(4000), nullable=False),
        Column("evidence_id", String(512), nullable=False),
        Column("dedup_key", String(64), nullable=False),
        Column("source_system", String(255), nullable=False, server_default="loki"),
        UniqueConstraint("incident_id", "dedup_key", name="uq_log_observation_incident_key"),
    )
    op.create_index("ix_log_observations_incident_id", "log_observations", ["incident_id"])
    op.create_index("ix_log_observations_event_at", "log_observations", ["event_at"])
    op.create_index("ix_log_observations_observed_at", "log_observations", ["observed_at"])


def downgrade() -> None:
    if "log_observations" in inspect(op.get_bind()).get_table_names():
        op.drop_table("log_observations")
