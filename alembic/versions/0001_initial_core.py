"""Initial deterministic core schema.

Revision ID: 0001_initial_core
Revises:
"""

from alembic import op

from packages.storage.models import Base

revision = "0001_initial_core"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create all v0.1.0 core tables from declarative metadata."""
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    """Drop all v0.1.0 core tables."""
    bind = op.get_bind()
    Base.metadata.drop_all(bind=bind)
