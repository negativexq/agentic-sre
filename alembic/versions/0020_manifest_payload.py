"""Freeze mutable source content (alerts) in the evidence manifest.

Revision ID: 0020_manifest_payload
Revises: 0019_capture_manifest_tape
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0020_manifest_payload"
down_revision = "0019_capture_manifest_tape"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {item["name"] for item in inspect(op.get_bind()).get_columns("run_evidence_manifest")}


def upgrade() -> None:
    # Revision 0001 creates tables from live metadata, so fresh databases may
    # already have this column while databases upgraded from older code do not.
    if "payload" not in _columns():
        op.add_column("run_evidence_manifest", sa.Column("payload", sa.JSON(), nullable=True))


def downgrade() -> None:
    if "payload" in _columns():
        op.drop_column("run_evidence_manifest", "payload")
