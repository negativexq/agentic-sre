"""Require a revision number and trigger on every diagnosis.

Revision ID: 0023_diagnosis_revision_required
Revises: 0022_diagnosis_revisions

Rows the pre-revision writer stored after 0022 carry no revision metadata.
They are pre-revision-aware LEGACY rows: appended per incident after the
current highest revision, in ``(created_at, diagnosis_id)`` order. Numbered
revisions are never renumbered, and no other provenance is fabricated.
"""

from itertools import groupby
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0023_diagnosis_revision_required"
down_revision = "0022_diagnosis_revisions"
branch_labels = None
depends_on = None

diagnoses = sa.table(
    "diagnoses",
    sa.column("diagnosis_id", sa.Integer()),
    sa.column("incident_id", sa.Uuid()),
    sa.column("created_at", sa.DateTime()),
    sa.column("revision_number", sa.Integer()),
    sa.column("trigger", sa.String(32)),
)


def _nullable() -> dict[str, bool]:
    return {
        column["name"]: bool(column["nullable"])
        for column in inspect(op.get_bind()).get_columns("diagnoses")
        if column["name"] in {"revision_number", "trigger"}
    }


def _append_gap_rows() -> None:
    connection = op.get_bind()
    highest: dict[Any, int] = {
        incident_id: number
        for incident_id, number in connection.execute(
            sa.select(diagnoses.c.incident_id, sa.func.max(diagnoses.c.revision_number))
            .where(diagnoses.c.revision_number.is_not(None))
            .group_by(diagnoses.c.incident_id)
        ).all()
    }
    gap = connection.execute(
        sa.select(diagnoses.c.diagnosis_id, diagnoses.c.incident_id)
        .where(diagnoses.c.revision_number.is_(None))
        .order_by(diagnoses.c.incident_id, diagnoses.c.created_at, diagnoses.c.diagnosis_id)
    ).all()
    for incident_id, rows in groupby(gap, key=lambda row: row.incident_id):
        start = highest.get(incident_id) or 0
        for offset, row in enumerate(rows, start=1):
            connection.execute(
                diagnoses.update()
                .where(diagnoses.c.diagnosis_id == row.diagnosis_id)
                .values(revision_number=start + offset, trigger="LEGACY")
            )
    connection.execute(
        diagnoses.update().where(diagnoses.c.trigger.is_(None)).values(trigger="LEGACY")
    )


def upgrade() -> None:
    _append_gap_rows()
    # Revision 0001 builds tables from live metadata: a fresh database is
    # already NOT NULL here.
    nullable = _nullable()
    if nullable.get("revision_number") or nullable.get("trigger"):
        with op.batch_alter_table("diagnoses") as batch:
            batch.alter_column("revision_number", existing_type=sa.Integer(), nullable=False)
            batch.alter_column("trigger", existing_type=sa.String(length=32), nullable=False)


def downgrade() -> None:
    nullable = _nullable()
    if not nullable.get("revision_number") or not nullable.get("trigger"):
        with op.batch_alter_table("diagnoses") as batch:
            batch.alter_column("revision_number", existing_type=sa.Integer(), nullable=True)
            batch.alter_column("trigger", existing_type=sa.String(length=32), nullable=True)
