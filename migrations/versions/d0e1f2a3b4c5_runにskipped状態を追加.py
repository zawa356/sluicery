"""Runにskipped状態を追加

Revision ID: d0e1f2a3b4c5
Revises: b8c9d0e1f2a3
Create Date: 2026-08-13 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d0e1f2a3b4c5"
down_revision: str | Sequence[str] | None = "b8c9d0e1f2a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_STATUS = sa.Enum(
    "running",
    "succeeded",
    "failed",
    "cancelled",
    name="run_status",
    native_enum=False,
    create_constraint=True,
)
_NEW_STATUS = sa.Enum(
    "running",
    "succeeded",
    "failed",
    "cancelled",
    "skipped",
    name="run_status",
    native_enum=False,
    create_constraint=True,
)


def upgrade() -> None:
    with op.batch_alter_table("run", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=_OLD_STATUS,
            type_=_NEW_STATUS,
            existing_nullable=False,
        )


def downgrade() -> None:
    op.execute("UPDATE run SET status = 'cancelled' WHERE status = 'skipped'")
    with op.batch_alter_table("run", schema=None) as batch_op:
        batch_op.alter_column(
            "status",
            existing_type=_NEW_STATUS,
            type_=_OLD_STATUS,
            existing_nullable=False,
        )
