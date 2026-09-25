"""Add diagnosis revision metadata and number legacy diagnoses.

Revision ID: 0022_diagnosis_revisions
Revises: 0021_query_key_text

Every column is nullable: the revision writer (M19-4.2) sets them. Existing
rows are numbered per incident in ``(created_at, diagnosis_id)`` order and
marked ``LEGACY``; no other historical metadata is reconstructed.
"""

from itertools import groupby
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0022_diagnosis_revisions"
down_revision = "0021_query_key_text"
branch_labels = None
depends_on = None

TRIGGERS = ("INITIAL", "MANUAL", "EVIDENCE_DEADLINE", "LEGACY")
UNIQUE = "uq_diagnosis_incident_revision"
CHECK = "ck_diagnosis_trigger"
PREVIOUS_FK = "fk_diagnoses_previous_diagnosis_id"
COLUMN_TYPES: tuple[tuple[str, sa.types.TypeEngine[Any]], ...] = (
    ("revision_number", sa.Integer()),
    ("previous_diagnosis_id", sa.Integer()),
    ("trigger", sa.String(length=32)),
    ("window_end", sa.DateTime()),
    ("manifest_digest", sa.String(length=64)),
    ("tape_digest", sa.String(length=64)),
    ("epistemic_digest", sa.String(length=64)),
    ("engine_version", sa.String(length=64)),
    ("config_digest", sa.String(length=64)),
)


def _check_sql() -> str:
    return '"trigger" IN (' + ", ".join(f"'{item}'" for item in TRIGGERS) + ")"


def _state() -> tuple[set[str], set[str | None], set[str | None], set[str | None]]:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("diagnoses")}
    uniques = {item["name"] for item in inspector.get_unique_constraints("diagnoses")}
    checks = {item["name"] for item in inspector.get_check_constraints("diagnoses")}
    foreign_keys = {item["name"] for item in inspector.get_foreign_keys("diagnoses")}
    return columns, uniques, checks, foreign_keys


def _backfill_legacy() -> None:
    """Number unnumbered rows 1..N per incident by (created_at, diagnosis_id); LEGACY."""
    connection = op.get_bind()
    diagnoses = sa.table(
        "diagnoses",
        sa.column("diagnosis_id", sa.Integer()),
        sa.column("incident_id", sa.Uuid()),
        sa.column("created_at", sa.DateTime()),
        sa.column("revision_number", sa.Integer()),
        sa.column("trigger", sa.String(32)),
    )
    rows = connection.execute(
        sa.select(diagnoses.c.diagnosis_id, diagnoses.c.incident_id)
        .where(diagnoses.c.revision_number.is_(None))
        .order_by(diagnoses.c.incident_id, diagnoses.c.created_at, diagnoses.c.diagnosis_id)
    ).all()
    for _incident, group in groupby(rows, key=lambda row: row.incident_id):
        for number, row in enumerate(group, start=1):
            connection.execute(
                diagnoses.update()
                .where(diagnoses.c.diagnosis_id == row.diagnosis_id)
                .values(revision_number=number, trigger="LEGACY")
            )


def upgrade() -> None:
    # Revision 0001 builds tables from live metadata, so a fresh database may
    # already have these columns and constraints; only add what is missing.
    columns, uniques, checks, foreign_keys = _state()
    missing = [(name, type_) for name, type_ in COLUMN_TYPES if name not in columns]
    if missing:
        with op.batch_alter_table("diagnoses") as batch:
            for name, type_ in missing:
                batch.add_column(sa.Column(name, type_, nullable=True))
    _backfill_legacy()
    with op.batch_alter_table("diagnoses") as batch:
        if UNIQUE not in uniques:
            batch.create_unique_constraint(UNIQUE, ["incident_id", "revision_number"])
        if CHECK not in checks:
            batch.create_check_constraint(CHECK, _check_sql())
        if PREVIOUS_FK not in foreign_keys:
            batch.create_foreign_key(
                PREVIOUS_FK, "diagnoses", ["previous_diagnosis_id"], ["diagnosis_id"]
            )


def downgrade() -> None:
    columns, uniques, checks, foreign_keys = _state()
    with op.batch_alter_table("diagnoses") as batch:
        if PREVIOUS_FK in foreign_keys:
            batch.drop_constraint(PREVIOUS_FK, type_="foreignkey")
        if CHECK in checks:
            batch.drop_constraint(CHECK, type_="check")
        if UNIQUE in uniques:
            batch.drop_constraint(UNIQUE, type_="unique")
        for name, _type in reversed(COLUMN_TYPES):
            if name in columns:
                batch.drop_column(name)
