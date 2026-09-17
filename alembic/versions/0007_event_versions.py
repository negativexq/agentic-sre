"""Kubernetes event journal.

Events were only ever read live from the cluster, never persisted. A resolved
incident's frozen window could not be re-read later once Kubernetes garbage
collected the events (about an hour by default), so re-diagnosing it later
could silently lose evidence. This adds an append-only journal for them,
mirroring the object version journal.

Revision ID: 0007_event_versions
Revises: 0006_object_lifecycle
"""

from alembic import op
from sqlalchemy import JSON, Column, DateTime, Integer, String, UniqueConstraint, inspect

revision = "0007_event_versions"
down_revision = "0006_object_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "event_versions" in tables:
        return
    op.create_table(
        "event_versions",
        Column("version_id", Integer(), primary_key=True, autoincrement=True),
        Column("namespace", String(255), nullable=False),
        Column("involved_kind", String(255), nullable=False),
        Column("involved_name", String(255), nullable=False),
        Column("dedup_key", String(512), nullable=False),
        Column("event_at", DateTime(timezone=True), nullable=False),
        Column("observed_at", DateTime(timezone=True), nullable=False),
        Column("body", JSON(), nullable=False),
        UniqueConstraint("namespace", "dedup_key"),
    )
    op.create_index("ix_event_versions_namespace", "event_versions", ["namespace"])
    op.create_index("ix_event_versions_event_at", "event_versions", ["event_at"])


def downgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "event_versions" in tables:
        op.drop_table("event_versions")
