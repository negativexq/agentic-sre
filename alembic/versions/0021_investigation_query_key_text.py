"""Store complete canonical provider query identities.

Revision ID: 0021_query_key_text
Revises: 0020_manifest_payload
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "0021_query_key_text"
down_revision = "0020_manifest_payload"
branch_labels = None
depends_on = None


def _query_key_type() -> object:
    column = next(
        item
        for item in inspect(op.get_bind()).get_columns("investigation_reads")
        if item["name"] == "query_key"
    )
    return column["type"]


def upgrade() -> None:
    if isinstance(_query_key_type(), sa.String) and not isinstance(_query_key_type(), sa.Text):
        with op.batch_alter_table("investigation_reads") as batch:
            batch.alter_column(
                "query_key",
                existing_type=sa.String(length=255),
                type_=sa.Text(),
                existing_nullable=False,
            )


def downgrade() -> None:
    if isinstance(_query_key_type(), sa.Text):
        with op.batch_alter_table("investigation_reads") as batch:
            batch.alter_column(
                "query_key",
                existing_type=sa.Text(),
                type_=sa.String(length=255),
                existing_nullable=False,
            )
