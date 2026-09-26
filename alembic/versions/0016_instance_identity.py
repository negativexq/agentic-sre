"""Persist Kubernetes UID identity on object and event versions.

Revision ID: 0016_instance_identity
Revises: 0015_investigation_run_artifacts
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0016_instance_identity"
down_revision = "0015_investigation_run_artifacts"
branch_labels = None
depends_on = None


def _nested_uid(body: object, parent_key: str) -> str | None:
    if not isinstance(body, dict):
        return None
    parent = body.get(parent_key)
    if not isinstance(parent, dict):
        return None
    uid = parent.get("uid")
    return uid if isinstance(uid, str) and uid else None


def _backfill_uids(table_name: str, uid_column: str, parent_key: str) -> None:
    connection = op.get_bind()
    table = sa.table(
        table_name,
        sa.column("version_id", sa.Integer()),
        sa.column("body", sa.JSON()),
        sa.column(uid_column, sa.String(64)),
    )
    rows = connection.execute(sa.select(table.c.version_id, table.c.body))
    for version_id, body in rows:
        uid = _nested_uid(body, parent_key)
        if uid is not None:
            connection.execute(
                table.update().where(table.c.version_id == version_id).values({uid_column: uid})
            )


def upgrade() -> None:
    # Revision 0001 creates tables from live metadata, so fresh databases may
    # already have these fields while databases upgraded from older code do not.
    inspector = inspect(op.get_bind())
    object_columns = {column["name"] for column in inspector.get_columns("object_versions")}
    event_columns = {column["name"] for column in inspector.get_columns("event_versions")}
    if "uid" not in object_columns:
        op.add_column("object_versions", sa.Column("uid", sa.String(length=64), nullable=True))
    if "involved_uid" not in event_columns:
        op.add_column(
            "event_versions", sa.Column("involved_uid", sa.String(length=64), nullable=True)
        )

    _backfill_uids("object_versions", "uid", "metadata")
    _backfill_uids("event_versions", "involved_uid", "involvedObject")

    index_names = {
        index["name"]
        for table_name in ("object_versions", "event_versions")
        for index in inspect(op.get_bind()).get_indexes(table_name)
    }
    if "ix_object_versions_namespace_kind_name_uid" not in index_names:
        op.create_index(
            "ix_object_versions_namespace_kind_name_uid",
            "object_versions",
            ["namespace", "kind", "name", "uid"],
        )
    if "ix_event_versions_namespace_involved_kind_name_involved_uid" not in index_names:
        op.create_index(
            "ix_event_versions_namespace_involved_kind_name_involved_uid",
            "event_versions",
            ["namespace", "involved_kind", "involved_name", "involved_uid"],
        )


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    event_indexes = {index["name"] for index in inspector.get_indexes("event_versions")}
    object_indexes = {index["name"] for index in inspector.get_indexes("object_versions")}
    if "ix_event_versions_namespace_involved_kind_name_involved_uid" in event_indexes:
        op.drop_index(
            "ix_event_versions_namespace_involved_kind_name_involved_uid",
            table_name="event_versions",
        )
    if "ix_object_versions_namespace_kind_name_uid" in object_indexes:
        op.drop_index(
            "ix_object_versions_namespace_kind_name_uid",
            table_name="object_versions",
        )

    event_columns = {
        column["name"] for column in inspect(op.get_bind()).get_columns("event_versions")
    }
    object_columns = {
        column["name"] for column in inspect(op.get_bind()).get_columns("object_versions")
    }
    if "involved_uid" in event_columns:
        op.drop_column("event_versions", "involved_uid")
    if "uid" in object_columns:
        op.drop_column("object_versions", "uid")
