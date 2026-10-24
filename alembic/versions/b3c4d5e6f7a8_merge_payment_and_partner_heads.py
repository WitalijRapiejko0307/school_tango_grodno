"""merge payment branch with couple-subscription head

Revision ID: b3c4d5e6f7a8
Revises: a7b8c9d0e1f2, f2a3b4c5d6e7
Create Date: 2026-10-24 14:40:00.000000

"""

from typing import Sequence, Union

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = ("a7b8c9d0e1f2", "f2a3b4c5d6e7")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
