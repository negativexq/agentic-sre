"""Scope email-delivery idempotency to (report_id, idempotency_key).

A global-unique idempotency key could return a delivery for a different report
when a client reused the key. Scoping the uniqueness to the report fixes that and
still lets the same key be reused across reports.

Revision ID: 0014_email_delivery_report_key
Revises: 0013_email_deliveries
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0014_email_delivery_report_key"
down_revision = "0013_email_deliveries"
branch_labels = None
depends_on = None

_COLUMNS = (
    "delivery_id, report_id, incident_id, recipients, subject, status, error, "
    "idempotency_key, created_at"
)


def _unique_names(table: str) -> set[str]:
    inspector = inspect(op.get_bind())
    if table not in inspector.get_table_names():
        return set()
    return {uc["name"] for uc in inspector.get_unique_constraints(table) if uc["name"]}


def _rebuild(unique_columns: list[str], name: str) -> None:
    op.create_table(
        "email_deliveries_tmp",
        sa.Column("delivery_id", sa.String(64), primary_key=True),
        sa.Column("report_id", sa.String(64), nullable=False),
        sa.Column("incident_id", sa.Uuid(), nullable=True),
        sa.Column("recipients", sa.JSON(), nullable=False),
        sa.Column("subject", sa.String(512), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("error", sa.String(1000), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint(*unique_columns, name=name),
    )
    op.execute(
        f"INSERT INTO email_deliveries_tmp ({_COLUMNS}) SELECT {_COLUMNS} FROM email_deliveries"
    )
    op.drop_table("email_deliveries")
    op.rename_table("email_deliveries_tmp", "email_deliveries")
    op.create_index("ix_email_deliveries_report_id", "email_deliveries", ["report_id"])


def upgrade() -> None:
    if "uq_email_delivery_report_key" in _unique_names("email_deliveries"):
        return
    _rebuild(["report_id", "idempotency_key"], "uq_email_delivery_report_key")


def downgrade() -> None:
    if "uq_email_delivery_report_key" not in _unique_names("email_deliveries"):
        return
    _rebuild(["idempotency_key"], "uq_email_delivery_idempotency_key")
