"""Object journal keeps repeated content and records lifecycle.

The unique (object_key, content_hash) constraint rejected A -> B -> A, which a
rollback produces. The table is rebuilt without it, with a lifecycle column
(OBSERVED, CREATED, UPDATED, DELETED). Rebuilding avoids dialect-specific
names for the unnamed constraint.

Revision ID: 0006_object_lifecycle
Revises: 0005_object_versions_diagnoses
"""

from alembic import op
from sqlalchemy import JSON, Column, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.sql.schema import SchemaItem

revision = "0006_object_lifecycle"
down_revision = "0005_object_versions_diagnoses"
branch_labels = None
depends_on = None

_COLUMNS = "version_id, object_key, namespace, kind, name, observed_at, content_hash, body"


def _create(name: str, *, lifecycle: bool, unique: bool) -> None:
    columns: list[SchemaItem] = [
        Column("version_id", Integer(), primary_key=True, autoincrement=True),
        Column("object_key", String(512), nullable=False),
        Column("namespace", String(255), nullable=False),
        Column("kind", String(255), nullable=False),
        Column("name", String(255), nullable=False),
        Column("observed_at", DateTime(timezone=True), nullable=False),
        Column("content_hash", String(64), nullable=False),
        Column("body", JSON(), nullable=False),
    ]
    if lifecycle:
        columns.append(Column("lifecycle", String(16), nullable=False, server_default="UPDATED"))
    if unique:
        columns.append(UniqueConstraint("object_key", "content_hash"))
    op.create_table(name, *columns)


def _swap(new: str) -> None:
    op.drop_index("ix_object_versions_object_key", table_name="object_versions")
    op.drop_index("ix_object_versions_observed_at", table_name="object_versions")
    op.drop_table("object_versions")
    op.rename_table(new, "object_versions")
    op.create_index("ix_object_versions_object_key", "object_versions", ["object_key"])
    op.create_index("ix_object_versions_observed_at", "object_versions", ["observed_at"])


def upgrade() -> None:
    _create("object_versions_v6", lifecycle=True, unique=False)
    op.execute(
        f"INSERT INTO object_versions_v6 ({_COLUMNS}, lifecycle) "
        f"SELECT {_COLUMNS}, 'UPDATED' FROM object_versions"
    )
    _swap("object_versions_v6")


def downgrade() -> None:
    # Repeated content and tombstones cannot satisfy the old constraint; keep the
    # first row per (object, content) and drop tombstones.
    _create("object_versions_v5", lifecycle=False, unique=True)
    op.execute(
        f"INSERT INTO object_versions_v5 ({_COLUMNS}) "
        f"SELECT {_COLUMNS} FROM object_versions WHERE lifecycle <> 'DELETED' "
        "AND version_id IN (SELECT MIN(version_id) FROM object_versions "
        "WHERE lifecycle <> 'DELETED' GROUP BY object_key, content_hash)"
    )
    _swap("object_versions_v5")
