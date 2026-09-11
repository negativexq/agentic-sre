"""Workload service tables.

Revision ID: 0002_workload_tables
Revises: 0001_initial_core
"""

from alembic import op
from workload.common.models import WorkloadBase

revision = "0002_workload_tables"
down_revision = "0001_initial_core"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create order and payment tables."""
    WorkloadBase.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    """Drop order and payment tables."""
    WorkloadBase.metadata.drop_all(bind=op.get_bind())
