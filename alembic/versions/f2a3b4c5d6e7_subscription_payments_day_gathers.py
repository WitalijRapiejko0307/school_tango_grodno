"""subscription billing, payments, day_gathers, contact person_id

Revision ID: f2a3b4c5d6e7
Revises: e0f1a2b3c4d5
Create Date: 2026-10-24 12:00:00.000000

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, Sequence[str], None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("starts_on", sa.Date(), nullable=True))
    op.add_column(
        "subscriptions",
        sa.Column("amount_due", sa.Numeric(10, 2), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "amount_paid",
            sa.Numeric(10, 2),
            nullable=False,
            server_default="0",
        ),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("subscription_id", sa.String(length=36), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("paid_on", sa.Date(), nullable=False),
        sa.Column("recorded_by_id", sa.String(length=36), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["recorded_by_id"], ["persons.id"]),
        sa.ForeignKeyConstraint(["subscription_id"], ["subscriptions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    op.add_column(
        "contact_cards",
        sa.Column("person_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_contact_cards_person_id_persons",
        "contact_cards",
        "persons",
        ["person_id"],
        ["id"],
    )

    op.create_table(
        "day_gathers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("stream_id", sa.String(length=36), nullable=False),
        sa.Column("gather_date", sa.Date(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "response",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
        sa.Column("selected_session_ids", sa.Text(), nullable=True),
        sa.Column("nudge_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["person_id"], ["persons.id"]),
        sa.ForeignKeyConstraint(["stream_id"], ["streams.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "person_id",
            "stream_id",
            "gather_date",
            name="uq_day_gathers_person_stream_date",
        ),
    )


def downgrade() -> None:
    op.drop_table("day_gathers")
    op.drop_constraint(
        "fk_contact_cards_person_id_persons",
        "contact_cards",
        type_="foreignkey",
    )
    op.drop_column("contact_cards", "person_id")
    op.drop_table("payments")
    op.drop_column("subscriptions", "amount_paid")
    op.drop_column("subscriptions", "amount_due")
    op.drop_column("subscriptions", "starts_on")
