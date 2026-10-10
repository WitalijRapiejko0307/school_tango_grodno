"""attendance marked_by

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
Create Date: 2026-10-10 11:30:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, Sequence[str], None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "attendances",
        sa.Column("marked_by_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_attendances_marked_by_id_persons",
        "attendances",
        "persons",
        ["marked_by_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_attendances_marked_by_id_persons", "attendances", type_="foreignkey"
    )
    op.drop_column("attendances", "marked_by_id")
