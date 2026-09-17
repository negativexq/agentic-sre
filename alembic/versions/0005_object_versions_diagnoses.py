"""Object version journal and stored diagnoses.

Revision ID: 0005_object_versions_diagnoses
Revises: 0004_change_scope
"""

from alembic import op
from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    inspect,
)

revision = "0005_object_versions_diagnoses"
down_revision = "0004_change_scope"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the object journal read by diagnosis and the diagnosis store."""
    tables = set(inspect(op.get_bind()).get_table_names())
    if "object_versions" not in tables:
        op.create_table(
            "object_versions",
            Column("version_id", Integer(), primary_key=True, autoincrement=True),
            Column("object_key", String(512), nullable=False),
            Column("namespace", String(255), nullable=False),
            Column("kind", String(255), nullable=False),
            Column("name", String(255), nullable=False),
            Column("observed_at", DateTime(timezone=True), nullable=False),
            Column("content_hash", String(64), nullable=False),
            Column("body", JSON(), nullable=False),
            UniqueConstraint("object_key", "content_hash"),
        )
        op.create_index("ix_object_versions_object_key", "object_versions", ["object_key"])
        op.create_index("ix_object_versions_observed_at", "object_versions", ["observed_at"])
    if "diagnoses" not in tables:
        op.create_table(
            "diagnoses",
            Column("diagnosis_id", Integer(), primary_key=True, autoincrement=True),
            Column(
                "incident_id",
                Uuid(),
                ForeignKey("incidents.incident_id", ondelete="CASCADE"),
                nullable=False,
            ),
            Column("created_at", DateTime(timezone=True), nullable=False),
            Column("root_cause", String(512), nullable=True),
            Column("confidence", String(32), nullable=False),
            Column("mode", String(64), nullable=False),
            Column("document", JSON(), nullable=False),
        )
        op.create_index("ix_diagnoses_incident_id", "diagnoses", ["incident_id"])


def downgrade() -> None:
    """Drop the diagnosis store and object journal."""
    tables = set(inspect(op.get_bind()).get_table_names())
    if "diagnoses" in tables:
        op.drop_table("diagnoses")
    if "object_versions" in tables:
        op.drop_table("object_versions")
