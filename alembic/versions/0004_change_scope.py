"""Add factual deployment/configuration scope to change records.

Revision ID: 0004_change_scope
Revises: 0003_change_records
"""

from alembic import op
from sqlalchemy import Column, String, inspect, text

revision = "0004_change_scope"
down_revision = "0003_change_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add a backwards-compatible factual scope to the immutable journal."""
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("change_records")}
    if "scope" not in columns:
        op.add_column(
            "change_records",
            Column("scope", String(64), nullable=False, server_default=text("'DEPLOYMENT'")),
        )


def downgrade() -> None:
    """Remove the scope column when rolling back the journal extension."""
    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("change_records")}
    if "scope" in columns:
        op.drop_column("change_records", "scope")
