"""Allow the RESOLVED diagnosis trigger.

Revision ID: 0030_resolved_trigger
Revises: 0029_stream_follow_segments

Only a change of an incident starts a diagnosis, under a trigger that names the change; its resolution is
diagnosed under RESOLVED (docs/architecture/diagnosis-trigger-design.md, §2).
"""

from alembic import op

revision = "0030_resolved_trigger"
down_revision = "0029_stream_follow_segments"
branch_labels = None
depends_on = None

CHECK = "ck_diagnosis_trigger"
BEFORE = ("INITIAL", "MANUAL", "EVIDENCE_DEADLINE", "LEGACY", "ALERT_REFIRED")
AFTER = (*BEFORE, "RESOLVED")


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
