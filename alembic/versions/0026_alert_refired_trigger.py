"""Allow the ALERT_REFIRED diagnosis trigger.

Revision ID: 0026_alert_refired_trigger
Revises: 0025_alert_coverage

A re-firing alert that continues its episode is diagnosed again under its own trigger
(docs/architecture/incident-episode-contract.md, §4).
"""

from alembic import op

revision = "0026_alert_refired_trigger"
down_revision = "0025_alert_coverage"
branch_labels = None
depends_on = None

CHECK = "ck_diagnosis_trigger"
BEFORE = ("INITIAL", "MANUAL", "EVIDENCE_DEADLINE", "LEGACY")
AFTER = (*BEFORE, "ALERT_REFIRED")


def _check_sql(values: tuple[str, ...]) -> str:
    return '"trigger" IN (' + ", ".join(f"'{value}'" for value in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table("diagnoses") as batch:
        batch.drop_constraint(CHECK, type_="check")
        batch.create_check_constraint(CHECK, _check_sql(AFTER))


def downgrade() -> None:
    with op.batch_alter_table("diagnoses") as batch:
        batch.drop_constraint(CHECK, type_="check")
        batch.create_check_constraint(CHECK, _check_sql(BEFORE))
