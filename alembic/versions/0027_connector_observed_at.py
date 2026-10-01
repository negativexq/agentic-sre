"""Record when the Connector observed each journaled object version and Event.

Revision ID: 0027_connector_observed_at
Revises: 0026_alert_refired_trigger

A measurement table (connector contract §15), kept apart from the evidence rows: written only when a change
arrived through the Connector's stream, and read by no evidence window, eligibility or decision until
roadmap C9 decides it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0027_connector_observed_at"
down_revision = "0026_alert_refired_trigger"
branch_labels = None
depends_on = None

TABLE = "journal_arrivals"


def upgrade() -> None:
    # 0001 builds tables from the current models, so a fresh database may already have it
    if TABLE in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        TABLE,
        sa.Column("arrival_id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("journal", sa.String(length=16), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("connector_observed_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_journal_arrivals_row", TABLE, ["journal", "version_id"], unique=True)


def downgrade() -> None:
    if TABLE in inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_journal_arrivals_row", table_name=TABLE)
        op.drop_table(TABLE)
