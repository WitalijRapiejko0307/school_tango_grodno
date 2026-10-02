"""bot chat messages for Telegram cleanup

Revision ID: a1b2c3d4e5f6
Revises: 2fd443e345d3
Create Date: 2026-10-02 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "2fd443e345d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "bot_chat_messages",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("person_id", sa.String(length=36), nullable=False),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_message_id", sa.BigInteger(), nullable=False),
        sa.Column("school_session_id", sa.String(length=36), nullable=True),
        sa.Column("is_reply_menu_anchor", sa.Boolean(), nullable=False),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["person_id"], ["persons.id"]),
        sa.ForeignKeyConstraint(["school_session_id"], ["school_sessions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_bot_chat_messages_person_sent",
        "bot_chat_messages",
        ["person_id", "sent_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_bot_chat_messages_person_sent", table_name="bot_chat_messages")
    op.drop_table("bot_chat_messages")
