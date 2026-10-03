"""persons name_key for roster search

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
Create Date: 2026-10-03 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("persons", schema=None) as batch_op:
        batch_op.add_column(sa.Column("name_key", sa.String(length=255), nullable=True))
        batch_op.create_index("ix_persons_name_key", ["name_key"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("persons", schema=None) as batch_op:
        batch_op.drop_index("ix_persons_name_key")
        batch_op.drop_column("name_key")
