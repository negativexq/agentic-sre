"""Add the evidence requirements table.

Revision ID: 0024_evidence_requirements
Revises: 0023_diagnosis_revision_required

Schema only (M19-5.4): nothing writes this table yet. ``requirement_key`` and
``targets`` get their canonical meaning from the requirement writer (M19-5.5).
At most one row per ``requirement_key`` may be OPEN; closed rows for the same
key coexist as history.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0024_evidence_requirements"
down_revision = "0023_diagnosis_revision_required"
branch_labels = None
depends_on = None

TABLE = "evidence_requirements"
KINDS = ("STATUS_CONTINUITY", "RESOURCE_COVERAGE")
STATUSES = ("OPEN", "SATISFIED_BY_REVISION", "SUPERSEDED_BY_REVISION", "EXPIRED")
OPEN_KEY_INDEX = "uq_evidence_requirements_open_key"
INCIDENT_INDEX = "ix_evidence_requirements_incident_id"
_JSON_TYPE = {"postgresql": "json_typeof", "sqlite": "json_type"}


def _one_of(column: str, values: tuple[str, ...], name: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        f"{column} IN (" + ", ".join(f"'{item}'" for item in values) + ")", name=name
    )


def upgrade() -> None:
    # Revision 0001 creates tables from live metadata, so a fresh database may
    # already have this table while databases upgraded from 0023 do not.
    if TABLE in inspect(op.get_bind()).get_table_names():
        return
    array_check: list[sa.CheckConstraint] = []
    json_type = _JSON_TYPE.get(op.get_bind().dialect.name)
    if json_type is not None:
        array_check.append(
            sa.CheckConstraint(
                f"{json_type}(targets) = 'array'", name="ck_evidence_requirement_targets_array"
            )
        )
    op.create_table(
        TABLE,
        sa.Column("requirement_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("requirement_key", sa.String(64), nullable=False),
        sa.Column(
            "incident_id",
            sa.Uuid(),
            sa.ForeignKey("incidents.incident_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "diagnosis_id",
            sa.Integer(),
            sa.ForeignKey("diagnoses.diagnosis_id", name="fk_evidence_requirements_diagnosis_id"),
            nullable=False,
        ),
        sa.Column("hypothesis_key", sa.String(64), nullable=False),
        sa.Column("rule_id", sa.String(255), nullable=False),
        sa.Column("rule_version", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("targets", sa.JSON(), nullable=False),
        sa.Column("not_before", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        _one_of("kind", KINDS, "ck_evidence_requirement_kind"),
        _one_of("status", STATUSES, "ck_evidence_requirement_status"),
        *array_check,
    )
    op.create_index(INCIDENT_INDEX, TABLE, ["incident_id"])
    op.create_index(
        OPEN_KEY_INDEX,
        TABLE,
        ["requirement_key"],
        unique=True,
        postgresql_where=sa.text("status = 'OPEN'"),
        sqlite_where=sa.text("status = 'OPEN'"),
    )


def downgrade() -> None:
    if TABLE not in inspect(op.get_bind()).get_table_names():
        return
    indexes = {item["name"] for item in inspect(op.get_bind()).get_indexes(TABLE)}
    for name in (OPEN_KEY_INDEX, INCIDENT_INDEX):
        if name in indexes:
            op.drop_index(name, table_name=TABLE)
    op.drop_table(TABLE)
