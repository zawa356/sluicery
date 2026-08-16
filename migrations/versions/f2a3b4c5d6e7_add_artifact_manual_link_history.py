"""Artifactに手動リンク取消情報を追加

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-08-16 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f2a3b4c5d6e7"
down_revision: str | Sequence[str] | None = "e1f2a3b4c5d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("artifact", schema=None) as batch_op:
        batch_op.add_column(sa.Column("manual_link_previous_path", sa.String(2000)))
        batch_op.add_column(sa.Column("manual_linked_at", sa.DateTime()))


def downgrade() -> None:
    with op.batch_alter_table("artifact", schema=None) as batch_op:
        batch_op.drop_column("manual_linked_at")
        batch_op.drop_column("manual_link_previous_path")
