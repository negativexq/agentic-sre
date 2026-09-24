"""Add the exact-instance index and the append-only lifecycle observation ledger.

Revision ID: 0017_lifecycle_ledger
Revises: 0016_instance_identity
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0017_lifecycle_ledger"
down_revision = "0016_instance_identity"
branch_labels = None
depends_on = None

_TYPES = (
    "OBSERVED",
    "READY_TRUE",
    "READY_FALSE",
    "CONTAINER_STARTED",
    "CONTAINER_TERMINATED",
    "OOM_KILLED",
    "EVICTED",
    "DELETION_REQUESTED",
    "DELETED",
    "STATUS_SNAPSHOT",
)


def upgrade() -> None:
    # Revision 0001 creates tables from live metadata, so fresh databases may
    # already have these tables while databases upgraded from older code do not.
    tables = set(inspect(op.get_bind()).get_table_names())
    if "entity_instances" not in tables:
        op.create_table(
            "entity_instances",
            sa.Column("instance_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("namespace", sa.String(255), nullable=False),
            sa.Column("kind", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("uid", sa.String(64), nullable=False),
            sa.Column("owner_kind", sa.String(255), nullable=True),
            sa.Column("owner_name", sa.String(255), nullable=True),
            sa.Column("owner_uid", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("first_observed_at", sa.DateTime(), nullable=False),
            sa.Column("last_observed_at", sa.DateTime(), nullable=False),
            sa.Column("deleted_observed_at", sa.DateTime(), nullable=True),
            sa.UniqueConstraint("namespace", "kind", "uid", name="uq_entity_instance_uid"),
        )
        op.create_index(
            "ix_entity_instances_namespace_kind_name",
            "entity_instances",
            ["namespace", "kind", "name"],
        )
    if "lifecycle_observations" not in tables:
        op.create_table(
            "lifecycle_observations",
            sa.Column("observation_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("evidence_id", sa.String(512), nullable=False, unique=True),
            sa.Column("instance_uid", sa.String(64), nullable=False),
            sa.Column("namespace", sa.String(255), nullable=False),
            sa.Column("kind", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("type", sa.String(32), nullable=False),
            sa.Column("source_at", sa.DateTime(), nullable=True),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("ingested_at", sa.DateTime(), nullable=False),
            sa.Column("source", sa.String(64), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=False),
            sa.UniqueConstraint(
                "instance_uid",
                "type",
                "observed_at",
                "source",
                name="uq_lifecycle_observation_fact",
            ),
            sa.CheckConstraint(
                "type IN (" + ", ".join(f"'{item}'" for item in _TYPES) + ")",
                name="ck_lifecycle_observation_type",
            ),
        )
        op.create_index(
            "ix_lifecycle_observations_instance",
            "lifecycle_observations",
            ["namespace", "kind", "instance_uid"],
        )
        op.create_index(
            "ix_lifecycle_observations_namespace_observed",
            "lifecycle_observations",
            ["namespace", "observed_at"],
        )


def downgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "lifecycle_observations" in tables:
        op.drop_index(
            "ix_lifecycle_observations_namespace_observed", table_name="lifecycle_observations"
        )
        op.drop_index("ix_lifecycle_observations_instance", table_name="lifecycle_observations")
        op.drop_table("lifecycle_observations")
    if "entity_instances" in tables:
        op.drop_index("ix_entity_instances_namespace_kind_name", table_name="entity_instances")
        op.drop_table("entity_instances")
