"""Audit log of report shares over email.

Revision ID: 0013_email_deliveries
Revises: 0012_report_snapshots
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0013_email_deliveries"
down_revision = "0012_report_snapshots"
branch_labels = None
depends_on = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("email_deliveries"):
        return
    op.create_table(
        "email_deliveries",
        sa.Column("delivery_id", sa.String(64), primary_key=True),
        sa.Column("report_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.String(1000), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True, unique=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_email_deliveries_report_id", "email_deliveries", ["report_id"])


def downgrade() -> None:
    if _has_table("email_deliveries"):
        op.drop_index("ix_email_deliveries_report_id", table_name="email_deliveries")
        op.drop_table("email_deliveries")
