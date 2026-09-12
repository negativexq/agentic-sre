"""Immutable historical resource-change facts.

Revision ID: 0003_change_records
Revises: 0002_workload_tables
"""

from alembic import op
from sqlalchemy import JSON, Column, DateTime, String, Uuid

revision = "0003_change_records"
down_revision = "0002_workload_tables"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the change journal used by deterministic harnesses and readers."""
    op.create_table(
        "change_records",
        Column("change_id", Uuid(), primary_key=True),
        Column("timestamp", DateTime(timezone=True), nullable=False),
        Column("resource_type", String(255), nullable=False),
        Column("resource_name", String(255), nullable=False),
        Column("change_type", String(64), nullable=False),
        Column("before", JSON(), nullable=False),
        Column("after", JSON(), nullable=False),
        Column("revision", String(255), nullable=False),
        Column("source", String(255), nullable=False),
    )


def downgrade() -> None:
    """Drop the change journal."""
    op.drop_table("change_records")
