"""Allow one alert fingerprint to produce multiple incident episodes.

Revision ID: 0008_alert_occurrences
Revises: 0007_event_versions
"""

from alembic import op
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
)

revision = "0008_alert_occurrences"
down_revision = "0007_event_versions"
branch_labels = None
depends_on = None


def _alerts_table(metadata: MetaData) -> Table:
    """Describe the existing table without the v1.0 fingerprint uniqueness."""
    return Table(
        "alerts",
        metadata,
        Column("alert_id", Uuid(), primary_key=True),
        Column(
            "incident_id",
            Uuid(),
            ForeignKey("incidents.incident_id", ondelete="SET NULL"),
            nullable=True,
        ),
        Column("alert_name", String(255), nullable=False),
        Column("service", String(255), nullable=False),
        Column("namespace", String(255), nullable=False),
        Column("cluster", String(255), nullable=False),
        Column("starts_at", DateTime(timezone=True), nullable=False),
        Column("ends_at", DateTime(timezone=True), nullable=True),
        Column("labels", JSON(), nullable=False),
        Column("annotations", JSON(), nullable=False),
        Column("fingerprint", String(255), nullable=False),
        Column("status", String(32), nullable=False),
        Column("source", String(32), nullable=False),
    )


def upgrade() -> None:
    """Rebuild the small alert table so existing v1.0 data is preserved."""
    metadata = MetaData()
    with op.batch_alter_table("alerts", recreate="always", copy_from=_alerts_table(metadata)):
        pass


def downgrade() -> None:
    """Restore the old constraint only when historical data permits it."""
    metadata = MetaData()
    table = _alerts_table(metadata)
    table.append_constraint(UniqueConstraint("fingerprint", name="uq_alerts_fingerprint"))
    with op.batch_alter_table("alerts", recreate="always", copy_from=table):
        pass
