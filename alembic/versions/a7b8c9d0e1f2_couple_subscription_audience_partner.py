"""couple subscription audience and partner_id

Revision ID: a7b8c9d0e1f2
Revises: f1a2b3c4d5e6
Create Date: 2026-10-16 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, Sequence[str], None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "products",
        sa.Column(
            "audience",
            sa.String(length=16),
            nullable=False,
            server_default="person",
        ),
    )
    op.execute(
        sa.text(
            "UPDATE products SET audience = 'couple' WHERE name ILIKE '%семейная пара%'"
        )
    )
    op.add_column(
        "subscriptions",
        sa.Column("partner_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_subscriptions_partner_id_persons",
        "subscriptions",
        "persons",
        ["partner_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_subscriptions_partner_id_persons", "subscriptions", type_="foreignkey"
    )
    op.drop_column("subscriptions", "partner_id")
    op.drop_column("products", "audience")
