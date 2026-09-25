"""Record when evidence rows were ingested, and a nullable source time for object versions.

Revision ID: 0018_temporal_provenance
Revises: 0017_lifecycle_ledger

``ingested_at`` is provenance only. Existing rows are backfilled from the time
the collector saw them (``change_records`` from their own ``timestamp``). The
object version ``source_at`` is left NULL: an observation time is not a source
time, so nothing is invented for rows that never stated one.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0018_temporal_provenance"
down_revision = "0017_lifecycle_ledger"
branch_labels = None
depends_on = None

# table -> (new column, backfill source column or None)
_COLUMNS: tuple[tuple[str, str, str | None], ...] = (
    ("object_versions", "ingested_at", "observed_at"),
    ("object_versions", "source_at", None),
    ("event_versions", "ingested_at", "observed_at"),
    ("change_records", "ingested_at", "timestamp"),
    ("log_observations", "ingested_at", "observed_at"),
)


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    # Revision 0001 creates tables from live metadata, so fresh databases may
    # already have these fields while databases upgraded from older code do not.
    for table, column, backfill in _COLUMNS:
        if column in _columns(table):
            continue
        op.add_column(table, sa.Column(column, sa.DateTime(), nullable=True))
        if backfill is not None:
            target = sa.table(table, sa.column(column), sa.column(backfill))
            op.execute(
                target.update()
                .where(target.c[column].is_(None))
                .values({column: target.c[backfill]})
            )


def downgrade() -> None:
    for table, column, _ in reversed(_COLUMNS):
        if column in _columns(table):
            op.drop_column(table, column)
