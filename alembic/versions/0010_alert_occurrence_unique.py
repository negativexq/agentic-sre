"""Enforce one persisted Alertmanager occurrence per fingerprint and start time.

Revision ID: 0010_alert_occurrence_unique
Revises: 0009_log_observations
"""

from alembic import op
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    Uuid,
    inspect,
    text,
)

revision = "0010_alert_occurrence_unique"
down_revision = "0009_log_observations"
branch_labels = None
depends_on = None

_CONSTRAINT = "uq_alert_occurrence_fingerprint_starts_at"


def _alerts_table(metadata: MetaData, *, unique: bool) -> Table:
    constraints = [UniqueConstraint("fingerprint", "starts_at", name=_CONSTRAINT)] if unique else []
    table = Table(
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
        *constraints,
    )
    Index("ix_alerts_fingerprint", table.c.fingerprint)
    return table


def upgrade() -> None:
    """Fail clearly on pre-existing duplicates, then add the DB invariant."""
    bind = op.get_bind()
    duplicates = bind.execute(
        text(
            "SELECT fingerprint, starts_at, COUNT(*) AS occurrences "
            "FROM alerts GROUP BY fingerprint, starts_at HAVING COUNT(*) > 1"
        )
    ).all()
    if duplicates:
        keys = ", ".join(f"{row[0]} @ {row[1]} ({row[2]})" for row in duplicates[:5])
        raise RuntimeError(
            "cannot add alert occurrence uniqueness; duplicate rows require manual review: " + keys
        )
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "alerts", recreate="always", copy_from=_alerts_table(MetaData(), unique=True)
        ):
            pass
    elif _CONSTRAINT not in {
        item["name"] for item in inspect(bind).get_unique_constraints("alerts")
    }:
        op.create_unique_constraint(_CONSTRAINT, "alerts", ["fingerprint", "starts_at"])


def downgrade() -> None:
    """Remove the database invariant while retaining occurrence rows."""
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table(
            "alerts", recreate="always", copy_from=_alerts_table(MetaData(), unique=False)
        ):
            pass
    elif _CONSTRAINT in {item["name"] for item in inspect(bind).get_unique_constraints("alerts")}:
        op.drop_constraint(_CONSTRAINT, "alerts", type_="unique")
