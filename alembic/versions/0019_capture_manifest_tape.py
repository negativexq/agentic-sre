"""Add the snapshot cycle, run evidence manifest and provider read tape tables.

Revision ID: 0019_capture_manifest_tape
Revises: 0018_temporal_provenance

Schema only: nothing writes these tables yet (M19-3.4, M19-3.6, M19-3.9).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0019_capture_manifest_tape"
down_revision = "0018_temporal_provenance"
branch_labels = None
depends_on = None

_CALLER_CLASSES = ("CAPTURE", "ENGINE", "INVESTIGATION")
_STATUSES = ("SUCCESS", "ERROR")
_FK_NAME = "fk_log_observations_source_read_id"


def _one_of(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IN (" + ", ".join(f"'{item}'" for item in values) + ")", name=name
    )


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # Revision 0001 creates tables from live metadata, so fresh databases may
    # already have these tables while databases upgraded from older code do not.
    tables = _tables()
    if "snapshot_cycles" not in tables:
        op.create_table(
            "snapshot_cycles",
            sa.Column("cycle_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(64), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("observed_at", sa.DateTime(), nullable=False),
            sa.Column("completed_at", sa.DateTime(), nullable=False),
            sa.Column("completed_scopes", sa.JSON(), nullable=False),
            sa.Column("failed_scopes", sa.JSON(), nullable=False),
        )
        op.create_index("ix_snapshot_cycles_run_id", "snapshot_cycles", ["run_id"])
    if "snapshot_cycle_objects" not in tables:
        op.create_table(
            "snapshot_cycle_objects",
            sa.Column(
                "cycle_id",
                sa.Integer(),
                sa.ForeignKey("snapshot_cycles.cycle_id"),
                primary_key=True,
            ),
            sa.Column("object_key", sa.String(512), primary_key=True),
            sa.Column("namespace", sa.String(255), nullable=False),
            sa.Column("kind", sa.String(255), nullable=False),
            sa.Column("name", sa.String(255), nullable=False),
            sa.Column("uid", sa.String(64), nullable=True),
            sa.Column("body", sa.JSON(), nullable=False),
            sa.Column("evidence_id", sa.String(600), nullable=False, unique=True),
        )
    if "run_evidence_manifest" not in tables:
        op.create_table(
            "run_evidence_manifest",
            sa.Column("manifest_entry_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(64), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_id", sa.String(600), nullable=False),
            sa.UniqueConstraint(
                "run_id", "source_type", "source_id", name="uq_manifest_run_source"
            ),
            sa.UniqueConstraint("run_id", "sequence", name="uq_manifest_run_sequence"),
        )
    if "investigation_reads" not in tables:
        op.create_table(
            "investigation_reads",
            sa.Column("read_id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("run_id", sa.String(64), nullable=False),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("caller_class", sa.String(16), nullable=False),
            sa.Column("capability", sa.String(64), nullable=False),
            sa.Column("query_key", sa.String(255), nullable=False),
            sa.Column("query_descriptor", sa.JSON(), nullable=False),
            sa.Column("started_at", sa.DateTime(), nullable=False),
            sa.Column("finished_at", sa.DateTime(), nullable=False),
            sa.Column("committed_at", sa.DateTime(), nullable=True),
            sa.Column("status", sa.String(8), nullable=False),
            sa.Column("observation", sa.JSON(), nullable=True),
            sa.Column("evidence_ids", sa.JSON(), nullable=False),
            sa.Column("error_type", sa.String(255), nullable=True),
            sa.Column("error_message", sa.String(4000), nullable=True),
            sa.UniqueConstraint("run_id", "sequence", name="uq_investigation_read_run_sequence"),
            _one_of("caller_class", _CALLER_CLASSES, "ck_investigation_read_caller_class"),
            _one_of("status", _STATUSES, "ck_investigation_read_status"),
        )
    columns = {item["name"] for item in inspect(op.get_bind()).get_columns("log_observations")}
    if "source_read_id" not in columns:
        with op.batch_alter_table("log_observations") as batch:
            batch.add_column(sa.Column("source_read_id", sa.Integer(), nullable=True))
            batch.create_foreign_key(
                _FK_NAME, "investigation_reads", ["source_read_id"], ["read_id"]
            )


def downgrade() -> None:
    columns = {item["name"] for item in inspect(op.get_bind()).get_columns("log_observations")}
    if "source_read_id" in columns:
        with op.batch_alter_table("log_observations") as batch:
            batch.drop_constraint(_FK_NAME, type_="foreignkey")
            batch.drop_column("source_read_id")
    tables = _tables()
    for name in (
        "investigation_reads",
        "run_evidence_manifest",
        "snapshot_cycle_objects",
        "snapshot_cycles",
    ):
        if name in tables:
            op.drop_table(name)
