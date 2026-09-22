"""Bind a stored diagnosis to the pipeline run that produced it.

Revision ID: 0011_diagnosis_run_id
Revises: 0010_alert_occurrence_unique
"""

from alembic import op
from sqlalchemy import Column, String, inspect

revision = "0011_diagnosis_run_id"
down_revision = "0010_alert_occurrence_unique"
branch_labels = None
depends_on = None


def _has_column(table: str, column: str) -> bool:
    inspector = inspect(op.get_bind())
    return any(item["name"] == column for item in inspector.get_columns(table))


def upgrade() -> None:
    if not _has_column("diagnoses", "run_id"):
        op.add_column("diagnoses", Column("run_id", String(64), nullable=True))


def downgrade() -> None:
    if _has_column("diagnoses", "run_id"):
        op.drop_column("diagnoses", "run_id")
