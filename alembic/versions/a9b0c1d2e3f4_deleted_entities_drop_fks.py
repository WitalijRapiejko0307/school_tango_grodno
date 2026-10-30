"""deleted_entities; drop FKs to archivable entities

Revision ID: a9b0c1d2e3f4
Revises: b3c4d5e6f7a8
Create Date: 2026-10-30 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, Sequence[str], None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TARGETS = {"persons", "groups", "weekly_slots", "products", "prices"}


def upgrade() -> None:
    op.create_table(
        "deleted_entities",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("entity_type", sa.String(16), nullable=False),
        sa.Column("entity_id", sa.String(36), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "entity_type", "entity_id", name="uq_deleted_entities_ref"
        ),
    )
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        return
    inspector = sa.inspect(bind)
    for table in inspector.get_table_names():
        for fk in inspector.get_foreign_keys(table):
            if fk["referred_table"] in _TARGETS and fk.get("name"):
                op.drop_constraint(fk["name"], table, type_="foreignkey")


def downgrade() -> None:
    op.drop_table("deleted_entities")
